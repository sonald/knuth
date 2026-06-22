from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import Field

from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.runtime_events import (
    InsertPosition,
    MessageRewriteAnchorDraft,
    MessageRewriteMessageDraft,
)
from knuth.core.types import KnuthModel
from knuth_runtime.context import (
    MessageRewriteRecord,
    MessageTape,
    TapeAnchor,
    TapeItemSource,
    TapeMessage,
    reconstruct_message_tape_from_events,
    validate_provider_messages,
)
from knuth_runtime.ledger import RunLedger


class MessageMiddlewareCheckpoint(StrEnum):
    AFTER_USER_MESSAGE_COMMITTED = "after_user_message_committed"
    AFTER_TOOL_RESULT_COMMITTED = "after_tool_result_committed"
    AFTER_TURN_CLOSED = "after_turn_closed"
    BEFORE_MODEL_REQUEST = "before_model_request"


class MessageMiddlewareContext(KnuthModel):
    run_id: str
    checkpoint: MessageMiddlewareCheckpoint
    turn_start_id: str | None = None


class InsertPatch(KnuthModel):
    position: InsertPosition | None = None
    items: list[InferenceMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplacePatch(KnuthModel):
    target_ids: list[str]
    replacement_items: list[InferenceMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)


MessageTapePatch = InsertPatch | ReplacePatch


class MessageMiddleware(ABC):
    name: str
    priority: int = 100
    checkpoints: set[MessageMiddlewareCheckpoint]

    @abstractmethod
    async def process(
        self,
        ctx: MessageMiddlewareContext,
        messages: tuple[TapeMessage, ...],
    ) -> list[MessageTapePatch]:
        ...


_RESERVED_PATCH_METADATA_KEYS = frozenset(
    {
        "rewrite_id",
        "message_id",
        "origin",
        "visibility",
        "middleware",
        "suppresses",
        "operation",
        "position",
        "kind",
    }
)


def _semantic_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    reserved = sorted(set(metadata) & _RESERVED_PATCH_METADATA_KEYS)
    if reserved:
        raise ValueError(
            "message middleware metadata uses runtime-reserved keys: "
            + ", ".join(reserved)
        )
    if not metadata:
        return {}
    return {"semantic": dict(metadata)}


class MessageMiddlewareRunner:
    def __init__(
        self,
        ledger: RunLedger,
        middlewares: list[MessageMiddleware] | None = None,
    ) -> None:
        self.ledger = ledger
        seen: set[str] = set()
        duplicates: list[str] = []
        for middleware in middlewares or []:
            if middleware.name in seen:
                duplicates.append(middleware.name)
            seen.add(middleware.name)
        if duplicates:
            raise ValueError(
                "duplicate message middleware name: " + ", ".join(sorted(duplicates))
            )
        self.middlewares = [
            middleware
            for _, middleware in sorted(
                enumerate(middlewares or []),
                key=lambda item: (item[1].priority, item[0]),
            )
        ]

    async def run_checkpoint(
        self,
        run_id: str,
        checkpoint: MessageMiddlewareCheckpoint,
        *,
        turn_start_id: str | None = None,
    ) -> None:
        candidates = [
            middleware
            for middleware in self.middlewares
            if checkpoint in middleware.checkpoints
        ]
        if not candidates:
            return

        ctx = MessageMiddlewareContext(
            run_id=run_id,
            checkpoint=checkpoint,
            turn_start_id=turn_start_id,
        )
        events = await self.ledger.list_events(run_id)
        tape = await reconstruct_message_tape_from_events(events)
        for middleware in candidates:
            messages = tuple(tape.model_visible())
            patches = await middleware.process(ctx, messages)
            if not patches:
                continue
            self._validate_patch_plan(checkpoint, messages, patches)
            drafts = [
                draft
                for patch in patches
                for draft in self._compile_durable_patch(middleware, patch)
            ]
            await self.ledger.apply_many(run_id, drafts)
            events = await self.ledger.list_events(run_id)
            tape = await reconstruct_message_tape_from_events(events)

    def _validate_patch_plan(
        self,
        checkpoint: MessageMiddlewareCheckpoint,
        messages: tuple[TapeMessage, ...],
        patches: list[MessageTapePatch],
    ) -> None:
        visible_ids = {item.id for item in messages}
        replace_targets: set[str] = set()
        records: list[MessageRewriteRecord] = []

        for ordinal, patch in enumerate(patches):
            _semantic_metadata(patch.metadata)
            if isinstance(patch, ReplacePatch):
                if not patch.target_ids:
                    raise ValueError("replace patch requires target_ids")
                if len(set(patch.target_ids)) != len(patch.target_ids):
                    raise ValueError("replace patch target_ids must be unique")
                if not patch.replacement_items:
                    raise ValueError("replace patch requires replacement_items")
                missing = [
                    target_id
                    for target_id in patch.target_ids
                    if target_id not in visible_ids
                ]
                if missing:
                    raise ValueError(
                        "replace patch target must be in current projection: "
                        + ", ".join(missing)
                    )
                overlap = replace_targets & set(patch.target_ids)
                if overlap:
                    raise ValueError(
                        "replace patch target overlaps another patch: "
                        + ", ".join(sorted(overlap))
                    )
                replace_targets.update(patch.target_ids)
            else:
                if not patch.items:
                    raise ValueError("insert patch requires items")
                if patch.position is not None:
                    target_id = patch.position.target_id
                    if target_id not in visible_ids:
                        raise ValueError(
                            "insert patch target must be in current projection: "
                            + target_id
                        )
            records.append(self._candidate_record(patch, ordinal))

        candidate = MessageTape(items=list(messages)).with_records(records)
        provider_messages = candidate.model_context_messages()
        if checkpoint == MessageMiddlewareCheckpoint.AFTER_TOOL_RESULT_COMMITTED:
            validate_provider_messages(provider_messages, allow_open_tool_batch=True)
        else:
            validate_provider_messages(provider_messages)

    def _candidate_record(
        self,
        patch: MessageTapePatch,
        patch_ordinal: int,
    ) -> MessageRewriteRecord:
        if isinstance(patch, ReplacePatch):
            return MessageRewriteRecord(
                operation="replace",
                suppresses=list(patch.target_ids),
                messages=[
                    TapeAnchor(
                        id=f"candidate:{patch_ordinal}:anchor",
                        suppresses=list(patch.target_ids),
                    ),
                    *[
                        TapeMessage(
                            id=f"candidate:{patch_ordinal}#{index}",
                            message=item,
                            origin=TapeItemSource.MIDDLEWARE,
                        )
                        for index, item in enumerate(patch.replacement_items)
                    ],
                ],
            )
        return MessageRewriteRecord(
            operation="insert",
            position=patch.position,
            messages=[
                TapeMessage(
                    id=f"candidate:{patch_ordinal}#{index}",
                    message=item,
                    origin=TapeItemSource.MIDDLEWARE,
                )
                for index, item in enumerate(patch.items)
            ],
        )

    def _compile_durable_patch(
        self,
        middleware: MessageMiddleware,
        patch: MessageTapePatch,
    ):
        metadata = _semantic_metadata(patch.metadata)
        if isinstance(patch, ReplacePatch):
            begin = MessageRewriteAnchorDraft(
                kind="begin",
                middleware=middleware.name,
                operation="replace",
                suppresses=list(patch.target_ids),
            )
            messages = [
                MessageRewriteMessageDraft(
                    message=item,
                    metadata=metadata,
                )
                for item in patch.replacement_items
            ]
            end = MessageRewriteAnchorDraft(
                kind="end",
                middleware=middleware.name,
                operation="replace",
            )
            return [begin, *messages, end]
        begin = MessageRewriteAnchorDraft(
            kind="begin",
            middleware=middleware.name,
            operation="insert",
            position=patch.position,
        )
        messages = [
            MessageRewriteMessageDraft(
                message=item,
                metadata=metadata,
            )
            for item in patch.items
        ]
        end = MessageRewriteAnchorDraft(
            kind="end",
            middleware=middleware.name,
            operation="insert",
        )
        return [begin, *messages, end]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ObservationCondensationMiddleware(MessageMiddleware):
    name = "observation_condensation"
    priority = 50
    checkpoints = {
        MessageMiddlewareCheckpoint.AFTER_TOOL_RESULT_COMMITTED,
        MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST,
    }

    def __init__(self, *, max_chars: int = 4096, excerpt_chars: int = 1200) -> None:
        self.max_chars = max_chars
        self.excerpt_chars = excerpt_chars

    async def process(
        self,
        ctx: MessageMiddlewareContext,
        messages: tuple[TapeMessage, ...],
    ) -> list[MessageTapePatch]:
        patches: list[MessageTapePatch] = []
        _ = ctx
        for item in messages:
            if item.role != InferenceRole.TOOL_RESULT.value:
                continue
            if item.metadata.get("self_condensed"):
                continue
            content = item.content or ""
            if len(content) <= self.max_chars:
                continue
            digest = _sha256(content)
            replacement_content = (
                "Observation condensed for context headroom. Relevant excerpt:\n"
                + content[: self.excerpt_chars]
            )
            patches.append(
                ReplacePatch(
                    target_ids=[item.id],
                    replacement_items=[
                        InferenceMessage(
                            role=InferenceRole.TOOL_RESULT,
                            tool_call_id=item.tool_call_id,
                            tool_name=item.tool_name,
                            content=replacement_content,
                        )
                    ],
                    metadata={
                        "algorithm": "headroom_excerpt_v1",
                        "reason": "context_headroom",
                        "original_sha256": digest,
                        "original_chars": len(content),
                        "replacement_chars": len(replacement_content),
                    },
                )
            )
        return patches


class ContextCompactionMiddleware(MessageMiddleware):
    name = "context_compaction"
    priority = 100
    checkpoints = {
        MessageMiddlewareCheckpoint.AFTER_TURN_CLOSED,
        MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST,
    }

    def __init__(self, *, max_messages: int = 12, keep_last: int = 4) -> None:
        self.max_messages = max_messages
        self.keep_last = keep_last

    async def process(
        self,
        ctx: MessageMiddlewareContext,
        messages: tuple[TapeMessage, ...],
    ) -> list[MessageTapePatch]:
        # BEFORE_MODEL_REQUEST acts as the recovery fallback when the preferred
        # AFTER_TURN_CLOSED write was missed by a crash.
        _ = ctx
        visible = [
            item
            for item in messages
            if item.origin != TapeItemSource.MIDDLEWARE
        ]
        if len(visible) <= self.max_messages:
            return []
        candidate = visible[: max(0, len(visible) - self.keep_last)]
        if any(
            item.role == InferenceRole.TOOL_RESULT.value or item.tool_calls
            for item in candidate
        ):
            return []
        target_ids = [item.id for item in candidate]
        original = "\n".join((item.content or "").strip() for item in candidate)
        digest = _sha256(original)
        summary = "Earlier context summary:\n" + original[:2000]
        return [
            ReplacePatch(
                target_ids=target_ids,
                replacement_items=[
                    InferenceMessage(
                        role=InferenceRole.USER,
                        content=summary,
                    )
                ],
                metadata={
                    "algorithm": "deterministic_prefix_summary_v1",
                    "reason": "message_count",
                    "original_hash": digest,
                    "original_chars": len(original),
                    "replacement_chars": len(summary),
                },
            )
        ]
