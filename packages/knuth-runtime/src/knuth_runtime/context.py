from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from knuth.core.events import RuntimeEvent
from knuth.core.invocations import canonical_json
from knuth.core.messages import (
    InferenceMessage,
    InferenceRole,
    SystemSection,
    SystemSectionSource,
)
from knuth.core.runtime_events import (
    CheckpointTapeMessage,
    ContextSnapshot,
    InsertPosition,
    TapeItemSource,
    ledger_message_id,
)
from knuth.core.types import KnuthModel
from knuth_toold import ToolBroker

from knuth_runtime.ledger import RunLedger

_LOG = logging.getLogger(__name__)

_PREAMBLE_SEPARATOR = "\n\n"


def assemble_preamble(sections: list[SystemSection]) -> str | None:
    """Join section texts in the order given into a single preamble string.

    Order is the order sections are contributed (provider injection order), not
    a function of ``source``; empty texts are skipped, and an empty result is
    ``None`` so the runtime never sends a blank system message.
    """
    parts = [section.text for section in sections if section.text]
    if not parts:
        return None
    return _PREAMBLE_SEPARATOR.join(parts)


class RunContext(KnuthModel):
    run_id: str


class ContextView(KnuthModel):
    run_id: str
    messages: list[InferenceMessage]
    tools: list[dict[str, Any]]
    snapshot: ContextSnapshot | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class TapeMessage(KnuthModel):
    id: str
    message: InferenceMessage
    origin: TapeItemSource
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_inference_message(self) -> InferenceMessage:
        return self.message

    @property
    def role(self) -> str:
        return self.message.role.value

    @property
    def content(self) -> str | None:
        return self.message.content

    @property
    def tool_calls(self) -> list[Any]:
        return list(self.message.tool_calls)

    @property
    def tool_call_id(self) -> str | None:
        return self.message.tool_call_id

    @property
    def tool_name(self) -> str | None:
        return self.message.tool_name


class TapeAnchor(KnuthModel):
    # Stable debug label for an otherwise projection-only suppress marker.
    id: str
    suppresses: list[str] = Field(default_factory=list)


TapeItem = TapeMessage | TapeAnchor


class MessageTape(KnuthModel):
    items: list[TapeItem]

    def raw_ledger_messages(self) -> list[InferenceMessage]:
        return [
            item.to_inference_message()
            for item in self.items
            if isinstance(item, TapeMessage) and item.origin == TapeItemSource.LEDGER
        ]

    def model_visible(self) -> list[TapeMessage]:
        suppressed = {
            target_id
            for item in self.items
            if isinstance(item, TapeAnchor)
            for target_id in item.suppresses
        }
        return [
            item
            for item in self.items
            if isinstance(item, TapeMessage) and item.id not in suppressed
        ]

    def model_context_messages(self) -> list[InferenceMessage]:
        return [item.to_inference_message() for item in self.model_visible()]

    def with_records(self, records: list["MessageRewriteRecord"]) -> "MessageTape":
        return MessageTape(items=_apply_rewrite_records(self.items, records))


class MessageRewriteRecord(KnuthModel):
    operation: str
    position: InsertPosition | None = None
    suppresses: list[str] = Field(default_factory=list)
    messages: list[TapeItem] = Field(default_factory=list)


class SystemSectionProvider(ABC):
    """Additive seam: contributes ``SystemSection`` fragments to the preamble.

    Orthogonal to ``MessageMiddleware``, which rewrites the whole view. A
    provider only contributes; it has no power to alter messages or tools.
    """

    @abstractmethod
    async def sections(self, ctx: RunContext) -> list[SystemSection]:
        ...


class StaticSectionProvider(SystemSectionProvider):
    """Contributes one fixed section, or nothing when its text is empty."""

    def __init__(self, source: SystemSectionSource, text: str | None) -> None:
        self._source = source
        self._text = text

    async def sections(self, ctx: RunContext) -> list[SystemSection]:
        if not self._text:
            return []
        return [SystemSection(source=self._source, text=self._text)]


@runtime_checkable
class ContextRedactor(Protocol):
    """Redact stage seam: strips secrets before any other transformation sees
    the view. Runs first among the mutating stages by construction."""

    async def redact(self, ctx: RunContext, view: ContextView) -> ContextView:
        ...


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ContextBuilder:
    """Builds the model-facing view through fixed projection stages:

    assemble -> redact -> tool_filter -> freeze

    Durable message projection events are prepared by the runtime checkpoint
    runner before build(); this class only projects them.
    """

    def __init__(
        self,
        ledger: RunLedger,
        tool_broker: ToolBroker,
        section_providers: list[SystemSectionProvider] | None = None,
        redactor: ContextRedactor | None = None,
    ) -> None:
        self.ledger = ledger
        self.tool_broker = tool_broker
        self.section_providers = list(section_providers or [])
        self.redactor = redactor

    async def build(
        self,
        ctx: RunContext,
        *,
        model_config_fingerprint: str = "",
    ) -> ContextView:
        view = await self._assemble(ctx)
        if self.redactor is not None:
            view = await self.redactor.redact(ctx, view)
        view = await self._tool_filter(ctx, view)
        return self._freeze(view, model_config_fingerprint)

    async def _assemble(
        self,
        ctx: RunContext,
    ) -> ContextView:
        tape = await load_message_tape(self.ledger, ctx.run_id)
        messages = tape.model_context_messages()
        preamble = await self._preamble(ctx)
        if preamble:
            messages.insert(
                0,
                InferenceMessage(role=InferenceRole.SYSTEM, content=preamble),
            )
        messages = _merge_leading_system_messages(messages)
        validate_provider_messages(messages)
        return ContextView(run_id=ctx.run_id, messages=messages, tools=[])

    async def _preamble(self, ctx: RunContext) -> str | None:
        sections: list[SystemSection] = []
        for provider in self.section_providers:
            sections.extend(await provider.sections(ctx))
        return assemble_preamble(sections)

    async def _tool_filter(self, ctx: RunContext, view: ContextView) -> ContextView:
        tools = await self.tool_broker.list_visible_tools(ctx.run_id)
        return view.model_copy(update={"tools": tools})

    def _freeze(self, view: ContextView, model_config_fingerprint: str) -> ContextView:
        preamble = ""
        if view.messages and view.messages[0].role == InferenceRole.SYSTEM:
            preamble = view.messages[0].content or ""
        snapshot = ContextSnapshot(
            messages_hash=_sha256(
                canonical_json([message.model_dump() for message in view.messages])
            ),
            tools_hash=_sha256(canonical_json(view.tools)),
            preamble_hash=_sha256(preamble),
            model_config_hash=_sha256(model_config_fingerprint),
            message_count=len(view.messages),
            tool_count=len(view.tools),
        )
        return view.model_copy(update={"snapshot": snapshot})


async def project_messages_from_events(
    events: list[RuntimeEvent],
    *,
    allow_open_tool_batch: bool = False,
) -> list[InferenceMessage]:
    tape = await reconstruct_message_tape_from_events(events)
    messages = tape.model_context_messages()
    validate_provider_messages(messages, allow_open_tool_batch=allow_open_tool_batch)
    return messages


async def raw_ledger_messages_from_events(
    events: list[RuntimeEvent],
) -> list[InferenceMessage]:
    tape = await reconstruct_message_tape_from_events(events)
    return tape.raw_ledger_messages()


def _checkpoint_initial_tape(messages: tuple[CheckpointTapeMessage, ...]) -> MessageTape:
    """Materialize the checkpoint payload as the baseline tape for tail folds.

    Only model-visible items are stored, so no ``TapeAnchor`` is reconstructed:
    rewrites the checkpoint captured are already collapsed into the stored
    message sequence. Identity (``id``, ``origin``, ``metadata``) survives so
    middleware patches that target a pre-checkpoint message still match.
    """
    return MessageTape(
        items=[
            TapeMessage(
                id=entry.id,
                message=entry.message,
                origin=entry.origin,
                metadata=dict(entry.metadata),
            )
            for entry in messages
        ]
    )


async def load_message_tape(ledger: "RunLedger", run_id: str) -> MessageTape:
    """Shared read entry: latest valid checkpoint + tail fold, with full-replay
    fallback.

    The loader walks checkpoint candidates newest-first; corrupt or
    invalid-``through_seq`` records are skipped without a durable failure
    event, since the contract is that a bad cache only causes a performance
    regression, never a read failure. Bad-cache fallbacks are diagnostic
    logged (ADR-011 "第一版本只记录 diagnostics / debug log").
    """
    try:
        run = await ledger.get_run(run_id)
        run_last_seq = run.last_seq
    except KeyError:
        run_last_seq = None
    before_seq: int | None = None
    while True:
        checkpoint = await ledger.latest_message_projection_checkpoint(
            run_id, before_seq=before_seq
        )
        if checkpoint is None:
            break
        if checkpoint.through_seq < 0 or (
            run_last_seq is not None and checkpoint.through_seq > run_last_seq
        ):
            _LOG.debug(
                "projection checkpoint skipped: through_seq out of range",
                extra={
                    "run_id": run_id,
                    "checkpoint_seq": checkpoint.seq,
                    "through_seq": checkpoint.through_seq,
                    "run_last_seq": run_last_seq,
                },
            )
            before_seq = checkpoint.seq
            continue
        try:
            initial = _checkpoint_initial_tape(checkpoint.messages)
            tail = await ledger.list_message_projection_events(
                run_id, after_seq=checkpoint.through_seq
            )
            return await fold_message_tape(initial, tail)
        except Exception:
            # Bad checkpoint payload (schema break, fold error reading the
            # baseline): log diagnostic and try the next-older candidate. ADR
            # requires only perf regression, not a read failure.
            _LOG.warning(
                "projection checkpoint skipped: fast-path fold failed",
                extra={
                    "run_id": run_id,
                    "checkpoint_seq": checkpoint.seq,
                    "through_seq": checkpoint.through_seq,
                },
                exc_info=True,
            )
            before_seq = checkpoint.seq
            continue
    events = await ledger.list_message_projection_events(run_id)
    return await reconstruct_message_tape_from_events(events)


async def load_message_tape_without_checkpoint(
    ledger: "RunLedger",
    run_id: str,
    *,
    through_seq: int,
) -> MessageTape:
    """Writer-facing fold: replay raw projection events up to a fixed seq.

    Used by :class:`ProjectionCheckpointWriter` to produce the payload it then
    appends. Never consults existing checkpoints so a new cache write does not
    inherit corruption from an old one.
    """
    events = await ledger.list_message_projection_events(
        run_id, through_seq=through_seq
    )
    return await reconstruct_message_tape_from_events(events)


async def reconstruct_message_tape_from_events(
    events: list[RuntimeEvent],
) -> MessageTape:
    """Full-replay entry point: fold an event sequence onto an empty tape.

    Kept as a thin wrapper around :func:`fold_message_tape` so any callers that
    want a from-scratch fold do not have to know about the empty-initial
    convention.
    """
    return await fold_message_tape(MessageTape(items=[]), events)


async def fold_message_tape(
    initial: MessageTape,
    events: list[RuntimeEvent],
) -> MessageTape:
    """Conversation fold: a closed, typed mapping from decision events to the
    message sequence.

    The caller supplies an already-folded ``initial`` tape — typically empty
    for a full replay, or a checkpoint-derived prefix for the fast path. Tail
    folds intentionally ignore ``message.projection_checkpoint`` events so a
    full replay always agrees with the checkpoint fast path.
    """
    items: list[TapeItem] = list(initial.items)
    rewrite_records: list[MessageRewriteRecord] = []
    open_rewrites: dict[str, MessageRewriteRecord] = {}
    for event in events:
        if event.type == "message.projection_checkpoint":
            # Checkpoints are projection cache facts, not decision events; they
            # never modify the model-visible message sequence.
            continue
        if event.type == "user.message":
            items.append(
                TapeMessage(
                    id=ledger_message_id(event.seq),
                    message=InferenceMessage(
                        role=InferenceRole.USER, content=event.content
                    ),
                    origin=TapeItemSource.LEDGER,
                )
            )
        elif event.type == "model.completed":
            items.append(
                TapeMessage(
                    id=ledger_message_id(event.seq),
                    message=InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content=event.content,
                        tool_calls=list(event.tool_calls),
                    ),
                    origin=TapeItemSource.LEDGER,
                )
            )
        elif event.type == "tool.invocation_completed":
            items.append(
                TapeMessage(
                    id=ledger_message_id(event.seq),
                    message=InferenceMessage(
                        role=InferenceRole.TOOL_RESULT,
                        tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name,
                        content=event.observation,
                    ),
                    origin=TapeItemSource.LEDGER,
                    metadata={
                        "raw_artifacts": list(event.raw_artifacts),
                        "self_condensed": event.self_condensed,
                    },
                )
            )
        elif event.type == "conversation.notice":
            # A synthetic runtime notice is not human-authored, but every
            # provider must see it through the ordinary conversation channel, so
            # it projects as a user-role message at this (batch-closed) boundary.
            items.append(
                TapeMessage(
                    id=ledger_message_id(event.seq),
                    message=InferenceMessage(
                        role=InferenceRole.USER, content=event.content
                    ),
                    origin=TapeItemSource.LEDGER,
                )
            )
        elif event.type == "verification.failed":
            items.append(
                TapeMessage(
                    id=ledger_message_id(event.seq),
                    message=InferenceMessage(
                        role=InferenceRole.USER, content=event.feedback
                    ),
                    origin=TapeItemSource.LEDGER,
                )
            )
        elif event.type == "message.rewrite_anchor":
            if event.kind == "begin":
                anchor = TapeAnchor(
                    id=f"a:{event.seq}",
                    suppresses=list(event.suppresses),
                )
                open_rewrites[event.rewrite_id] = MessageRewriteRecord(
                    operation=event.operation,
                    position=event.position,
                    suppresses=list(event.suppresses),
                    messages=[anchor] if event.suppresses else [],
                )
            else:
                record = open_rewrites.pop(event.rewrite_id, None)
                if record is not None:
                    rewrite_records.append(record)
        elif event.type == "message.rewrite_message":
            record = open_rewrites.get(event.rewrite_id)
            if record is not None:
                record.messages.append(
                    TapeMessage(
                        id=event.message_id,
                        message=event.message,
                        origin=TapeItemSource.MIDDLEWARE,
                        metadata=dict(event.metadata),
                    )
                )
    return MessageTape(items=_apply_rewrite_records(items, rewrite_records))


def validate_provider_messages(
    messages: list[InferenceMessage],
    *,
    allow_open_tool_batch: bool = False,
) -> None:
    pending_tool_results: list[str] = []
    seen_non_system = False
    for message in messages:
        if message.role == InferenceRole.SYSTEM:
            if seen_non_system:
                raise ValueError("system message must be leading")
            continue
        seen_non_system = True
        if pending_tool_results and message.role != InferenceRole.TOOL_RESULT:
            raise ValueError("assistant tool calls must be followed by tool results")
        if message.role == InferenceRole.ASSISTANT:
            pending_tool_results.extend(call.effective_id for call in message.tool_calls)
        elif message.role == InferenceRole.TOOL_RESULT:
            if message.tool_call_id not in pending_tool_results:
                raise ValueError(
                    f"dangling tool result for tool_call_id={message.tool_call_id}"
                )
            pending_tool_results.remove(message.tool_call_id)
    if pending_tool_results and not allow_open_tool_batch:
        raise ValueError(
            "missing tool results for tool calls: " + ", ".join(pending_tool_results)
        )


def _merge_leading_system_messages(
    messages: list[InferenceMessage],
) -> list[InferenceMessage]:
    leading: list[InferenceMessage] = []
    rest_start = 0
    for idx, message in enumerate(messages):
        if message.role != InferenceRole.SYSTEM:
            rest_start = idx
            break
        leading.append(message)
    else:
        rest_start = len(messages)
    if len(leading) <= 1:
        return messages
    merged = assemble_preamble(
        [
            SystemSection(source=SystemSectionSource.BASE, text=message.content or "")
            for message in leading
        ]
    )
    return [
        InferenceMessage(role=InferenceRole.SYSTEM, content=merged),
        *messages[rest_start:],
    ]


def _apply_rewrite_records(
    base_items: list[TapeItem],
    records: list[MessageRewriteRecord],
) -> list[TapeItem]:
    items = list(base_items)
    for record in records:
        if record.operation == "replace":
            suppresses = set(record.suppresses)
            target_indexes = [
                idx
                for idx, item in enumerate(items)
                if isinstance(item, TapeMessage) and item.id in suppresses
            ]
            if not target_indexes:
                continue
            insert_at = min(target_indexes)
            items[insert_at:insert_at] = list(record.messages)
        else:
            insert_at = _position_index(items, record.position)
            items[insert_at:insert_at] = list(record.messages)
    return items


def _position_index(items: list[TapeItem], position: InsertPosition | None) -> int:
    if position is None:
        return len(items)
    for idx, item in enumerate(items):
        if isinstance(item, TapeMessage) and item.id == position.target_id:
            return idx if position.kind == "before" else idx + 1
    return len(items)
