from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from knuth.core.events import rewrite_message_id
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.runtime_events import (
    MessageRewriteAnchorDraft,
    MessageRewriteMessageDraft,
    TapePosition,
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
    AFTER_TOOL_RESULT_COMMITTED = "after_tool_result_committed"
    AFTER_TURN_CLOSED = "after_turn_closed"
    BEFORE_MODEL_REQUEST = "before_model_request"


class ContextBudget(KnuthModel):
    max_input_tokens: int
    reserved_output_tokens: int
    target_headroom_tokens: int


class MessageMiddlewareContext(KnuthModel):
    run_id: str
    checkpoint: MessageMiddlewareCheckpoint
    budget: ContextBudget | None = None


class InsertPatch(KnuthModel):
    operation: Literal["insert"] = "insert"
    position: TapePosition
    items: list[InferenceMessage]
    durable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplacePatch(KnuthModel):
    operation: Literal["replace"] = "replace"
    target_ids: list[str]
    replacement_items: list[InferenceMessage]
    durable: bool = True
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
        tape: MessageTape,
    ) -> list[MessageTapePatch]:
        ...


class MessageMiddlewareRunResult(KnuthModel):
    ephemeral_records: list[MessageRewriteRecord] = Field(default_factory=list)


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
        self.middlewares = sorted(middlewares or [], key=lambda item: item.priority)

    async def run_checkpoint(
        self,
        run_id: str,
        checkpoint: MessageMiddlewareCheckpoint,
        *,
        budget: ContextBudget | None = None,
    ) -> MessageMiddlewareRunResult:
        result = MessageMiddlewareRunResult()
        candidates = [
            middleware
            for middleware in self.middlewares
            if checkpoint in middleware.checkpoints
        ]
        if not candidates:
            return result

        ctx = MessageMiddlewareContext(
            run_id=run_id,
            checkpoint=checkpoint,
            budget=budget,
        )
        events = await self.ledger.list_events(run_id)
        tape = await reconstruct_message_tape_from_events(
            events, self.ledger.get_artifact_text
        )
        patch_ordinal = 0
        for middleware in candidates:
            patches = await middleware.process(ctx, tape)
            for patch in patches:
                record = self._ephemeral_record(
                    checkpoint, middleware, patch, patch_ordinal
                )
                patch_ordinal += 1
                self._validate_patch_application(checkpoint, tape, patch, record)
                if patch.durable:
                    await self.ledger.apply_many(
                        run_id, self._compile_durable_patch(middleware, patch)
                    )
                    events = await self.ledger.list_events(run_id)
                    tape = await reconstruct_message_tape_from_events(
                        events, self.ledger.get_artifact_text
                    )
                else:
                    result.ephemeral_records.append(record)
        if result.ephemeral_records:
            self._validate_ephemeral_records(checkpoint, tape, result.ephemeral_records)
        return result

    async def assert_checkpoint_complete(
        self,
        run_id: str,
        checkpoint: MessageMiddlewareCheckpoint,
        *,
        budget: ContextBudget | None = None,
    ) -> None:
        ctx = MessageMiddlewareContext(
            run_id=run_id,
            checkpoint=checkpoint,
            budget=budget,
        )
        events = await self.ledger.list_events(run_id)
        tape = await reconstruct_message_tape_from_events(
            events, self.ledger.get_artifact_text
        )
        for middleware in self.middlewares:
            ready = getattr(middleware, "assert_checkpoint_complete", None)
            if ready is not None:
                ready(ctx, tape)

    def _validate_patch_application(
        self,
        checkpoint: MessageMiddlewareCheckpoint,
        tape: MessageTape,
        patch: MessageTapePatch,
        record: MessageRewriteRecord,
    ) -> None:
        self._validate_patch_targets(tape, patch)
        candidate = tape.with_records([record])
        messages = candidate.model_context_messages()
        if checkpoint == MessageMiddlewareCheckpoint.AFTER_TOOL_RESULT_COMMITTED:
            validate_provider_messages(messages, allow_open_tool_batch=True)
        else:
            validate_provider_messages(messages)

    def _validate_ephemeral_records(
        self,
        checkpoint: MessageMiddlewareCheckpoint,
        tape: MessageTape,
        records: list[MessageRewriteRecord],
    ) -> None:
        candidate = tape.with_records(records)
        messages = candidate.model_context_messages()
        if checkpoint == MessageMiddlewareCheckpoint.AFTER_TOOL_RESULT_COMMITTED:
            validate_provider_messages(messages, allow_open_tool_batch=True)
        else:
            validate_provider_messages(messages)

    def _validate_patch_targets(
        self,
        tape: MessageTape,
        patch: MessageTapePatch,
    ) -> None:
        if not isinstance(patch, ReplacePatch):
            return
        if not patch.target_ids:
            raise ValueError("replace patch requires target_ids")
        existing = {item.id for item in tape.model_visible()}
        missing = [target_id for target_id in patch.target_ids if target_id not in existing]
        if missing:
            raise ValueError("replace patch target does not exist: " + ", ".join(missing))

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
                metadata=metadata,
            )
            messages = [
                MessageRewriteMessageDraft(
                    message=item,
                )
                for item in patch.replacement_items
            ]
            end = MessageRewriteAnchorDraft(
                kind="end",
                middleware=middleware.name,
                operation="replace",
                metadata={"_runtime": {"message_count": len(messages)}},
            )
            return [begin, *messages, end]
        begin = MessageRewriteAnchorDraft(
            kind="begin",
            middleware=middleware.name,
            operation="insert",
            position=patch.position,
            metadata=metadata,
        )
        messages = [
            MessageRewriteMessageDraft(
                message=item,
            )
            for item in patch.items
        ]
        end = MessageRewriteAnchorDraft(
            kind="end",
            middleware=middleware.name,
            operation="insert",
            metadata={"_runtime": {"message_count": len(messages)}},
        )
        return [begin, *messages, end]

    def _ephemeral_record(
        self,
        checkpoint: MessageMiddlewareCheckpoint,
        middleware: MessageMiddleware,
        patch: MessageTapePatch,
        patch_ordinal: int,
    ) -> MessageRewriteRecord:
        rewrite_id = f"eph:{checkpoint.value}:{patch_ordinal}"
        _semantic_metadata(patch.metadata)
        if isinstance(patch, ReplacePatch):
            return MessageRewriteRecord(
                rewrite_id=rewrite_id,
                operation="replace",
                middleware=middleware.name,
                suppresses=list(patch.target_ids),
                messages=[
                    TapeAnchor(
                        id=f"a:{rewrite_id}:begin",
                        suppresses=list(patch.target_ids),
                    ),
                    *[
                        TapeMessage(
                            id=rewrite_message_id(rewrite_id, index),
                            message=item,
                            origin=TapeItemSource.MIDDLEWARE,
                        )
                        for index, item in enumerate(patch.replacement_items)
                    ],
                ],
            )
        return MessageRewriteRecord(
            rewrite_id=rewrite_id,
            operation="insert",
            middleware=middleware.name,
            position=patch.position,
            messages=[
                *[
                    TapeMessage(
                        id=rewrite_message_id(rewrite_id, index),
                        message=item,
                        origin=TapeItemSource.MIDDLEWARE,
                    )
                    for index, item in enumerate(patch.items)
                ],
            ],
        )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ToolResultRedactionMiddleware(MessageMiddleware):
    name = "tool_result_redaction"
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
        tape: MessageTape,
    ) -> list[MessageTapePatch]:
        patches: list[MessageTapePatch] = []
        for item in tape.model_visible():
            if item.role != InferenceRole.TOOL_RESULT.value:
                continue
            content = item.content or ""
            if len(content) <= self.max_chars:
                continue
            digest = _sha256(content)
            replacement_content = (
                "Result redacted for context headroom. Relevant excerpt:\n"
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
                    durable=True,
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

    def assert_checkpoint_complete(
        self,
        ctx: MessageMiddlewareContext,
        tape: MessageTape,
    ) -> None:
        if ctx.checkpoint != MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST:
            return
        remaining = [
            item.id
            for item in tape.model_visible()
            if item.role == InferenceRole.TOOL_RESULT.value
            and len(item.content or "") > self.max_chars
        ]
        if remaining:
            raise ValueError(
                "tool result redaction incomplete before model request: "
                + ", ".join(remaining)
            )


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
        tape: MessageTape,
    ) -> list[MessageTapePatch]:
        # BEFORE_MODEL_REQUEST acts as the recovery fallback when the preferred
        # AFTER_TURN_CLOSED write was missed by a crash.
        visible = [
            item
            for item in tape.model_visible()
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
                durable=True,
                metadata={
                    "algorithm": "deterministic_prefix_summary_v1",
                    "reason": "message_count",
                    "original_hash": digest,
                    "original_chars": len(original),
                    "replacement_chars": len(summary),
                },
            )
        ]


class AgentsMDMiddleware(MessageMiddleware):
    name = "agents_md"
    priority = 10
    checkpoints = {MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST}

    def __init__(self, paths: list[Path | str]) -> None:
        self.paths = [Path(path) for path in paths]

    async def process(
        self,
        ctx: MessageMiddlewareContext,
        tape: MessageTape,
    ) -> list[MessageTapePatch]:
        parts: list[str] = []
        hashes: list[str] = []
        for path in self.paths:
            if not path.exists() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                continue
            digest = _sha256(text)
            hashes.append(digest)
            parts.append(f"# {path.name}\n{text}")
        if not parts:
            return []
        combined = "\n\n".join(parts)
        content_hash = _sha256(combined)
        return [
            InsertPatch(
                position=TapePosition(
                    kind="boundary",
                    boundary="conversation_start",
                ),
                items=[
                    InferenceMessage(
                        role=InferenceRole.SYSTEM,
                        content=combined,
                    )
                ],
                durable=False,
                metadata={
                    "content_hash": content_hash,
                    "source_hashes": hashes,
                },
            )
        ]
