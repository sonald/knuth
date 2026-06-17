from __future__ import annotations

import hashlib
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
from knuth.core.runtime_events import ContextSnapshot, TapePosition
from knuth.core.types import KnuthModel
from knuth_toold import ToolBroker

from knuth_runtime.ledger import RunLedger

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
    role: str
    content: str | None = None
    tool_calls: list[Any] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    origin: str
    source_event_seq: int | None = None
    middleware_name: str | None = None
    visibility: str = "model"
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_inference_message(self) -> InferenceMessage:
        return InferenceMessage(
            role=InferenceRole(self.role),
            content=self.content,
            tool_calls=list(self.tool_calls),
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
        )


class MessageTape(KnuthModel):
    items: list[TapeMessage]


class MessageRewriteRecord(KnuthModel):
    rewrite_id: str
    operation: str
    middleware: str
    position: TapePosition | None = None
    suppresses: list[str] = Field(default_factory=list)
    messages: list[TapeMessage] = Field(default_factory=list)


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

    Durable and ephemeral message rewrites are prepared by the runtime
    checkpoint runner before build(); this class only projects them.
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
        ephemeral_rewrite_records: list[MessageRewriteRecord] | None = None,
    ) -> ContextView:
        view = await self._assemble(ctx, ephemeral_rewrite_records or [])
        if self.redactor is not None:
            view = await self.redactor.redact(ctx, view)
        view = await self._tool_filter(ctx, view)
        return self._freeze(view, model_config_fingerprint)

    async def _assemble(
        self,
        ctx: RunContext,
        ephemeral_rewrite_records: list[MessageRewriteRecord],
    ) -> ContextView:
        events = await self.ledger.list_events(ctx.run_id)
        tape = await reconstruct_message_tape_from_events(
            events, self.ledger.get_artifact_text
        )
        if ephemeral_rewrite_records:
            tape = apply_rewrite_records_to_tape(tape, ephemeral_rewrite_records)
        messages = project_tape_messages(tape)
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


async def reconstruct_messages_from_events(
    events: list[RuntimeEvent],
    resolve_artifact_text,
) -> list[InferenceMessage]:
    messages: list[InferenceMessage] = []
    for event in events:
        if event.type == "user.message":
            messages.append(
                InferenceMessage(role=InferenceRole.USER, content=event.content)
            )
        elif event.type == "model.completed":
            messages.append(
                InferenceMessage(
                    role=InferenceRole.ASSISTANT,
                    content=event.content,
                    tool_calls=list(event.tool_calls),
                )
            )
        elif event.type == "tool.invocation_completed":
            observation = event.observation
            if observation is None and event.artifact_ref is not None:
                observation = await resolve_artifact_text(event.artifact_ref)
            messages.append(
                InferenceMessage(
                    role=InferenceRole.TOOL_RESULT,
                    tool_call_id=event.tool_call_id,
                    tool_name=event.tool_name,
                    content=observation or "",
                )
            )
        elif event.type == "conversation.notice":
            messages.append(
                InferenceMessage(role=InferenceRole.USER, content=event.content)
            )
        elif event.type == "verification.failed":
            messages.append(
                InferenceMessage(role=InferenceRole.USER, content=event.feedback)
            )
    return messages


async def project_messages_from_events(
    events: list[RuntimeEvent],
    resolve_artifact_text,
    *,
    allow_open_tool_batch: bool = False,
) -> list[InferenceMessage]:
    tape = await reconstruct_message_tape_from_events(events, resolve_artifact_text)
    messages = project_tape_messages(tape)
    validate_provider_messages(messages, allow_open_tool_batch=allow_open_tool_batch)
    return messages


async def reconstruct_message_tape_from_events(
    events: list[RuntimeEvent],
    resolve_artifact_text,
) -> MessageTape:
    """Conversation fold: a closed, typed mapping from decision events to the
    message sequence. Aggregate invariants guarantee the result is always a
    provider-valid sequence, so no defensive repair happens here."""
    items: list[TapeMessage] = []
    rewrite_records: list[MessageRewriteRecord] = []
    open_rewrites: dict[str, MessageRewriteRecord] = {}
    for event in events:
        if event.type == "user.message":
            items.append(
                TapeMessage(
                    id=f"m:{event.seq}",
                    role=InferenceRole.USER.value,
                    content=event.content,
                    origin="ledger",
                    source_event_seq=event.seq,
                )
            )
        elif event.type == "model.completed":
            items.append(
                TapeMessage(
                    id=f"m:{event.seq}",
                    role=InferenceRole.ASSISTANT.value,
                    content=event.content,
                    tool_calls=list(event.tool_calls),
                    origin="ledger",
                    source_event_seq=event.seq,
                )
            )
        elif event.type == "tool.invocation_completed":
            observation = event.observation
            if observation is None and event.artifact_ref is not None:
                observation = await resolve_artifact_text(event.artifact_ref)
            items.append(
                TapeMessage(
                    id=f"m:{event.seq}",
                    role=InferenceRole.TOOL_RESULT.value,
                    tool_call_id=event.tool_call_id,
                    tool_name=event.tool_name,
                    content=observation or "",
                    origin="ledger",
                    source_event_seq=event.seq,
                )
            )
        elif event.type == "conversation.notice":
            # A synthetic runtime notice is not human-authored, but every
            # provider must see it through the ordinary conversation channel, so
            # it projects as a user-role message at this (batch-closed) boundary.
            items.append(
                TapeMessage(
                    id=f"m:{event.seq}",
                    role=InferenceRole.USER.value,
                    content=event.content,
                    origin="ledger",
                    source_event_seq=event.seq,
                )
            )
        elif event.type == "verification.failed":
            items.append(
                TapeMessage(
                    id=f"m:{event.seq}",
                    role=InferenceRole.USER.value,
                    content=event.feedback,
                    origin="ledger",
                    source_event_seq=event.seq,
                )
            )
        elif event.type == "message.rewrite_anchor":
            anchor = TapeMessage(
                id=f"a:{event.seq}",
                role="internal_anchor",
                origin="middleware",
                source_event_seq=event.seq,
                middleware_name=event.middleware,
                visibility="internal",
                metadata={
                    **event.metadata,
                    "rewrite_id": event.rewrite_id,
                    "kind": event.kind,
                    "operation": event.operation,
                    "suppresses": list(event.suppresses),
                    "position": event.position.model_dump()
                    if event.position is not None
                    else None,
                },
            )
            if event.kind == "begin":
                open_rewrites[event.rewrite_id] = MessageRewriteRecord(
                    rewrite_id=event.rewrite_id,
                    operation=event.operation,
                    middleware=event.middleware,
                    position=event.position,
                    suppresses=list(event.suppresses),
                    messages=[anchor],
                )
            else:
                record = open_rewrites.pop(event.rewrite_id, None)
                if record is not None:
                    record.messages.append(anchor)
                    rewrite_records.append(record)
        elif event.type == "message.rewrite_message":
            record = open_rewrites.get(event.rewrite_id)
            if record is not None:
                record.messages.append(
                    TapeMessage(
                        id=event.message_id,
                        role=event.message.role.value,
                        content=event.message.content,
                        tool_calls=list(event.message.tool_calls),
                        tool_call_id=event.message.tool_call_id,
                        tool_name=event.message.tool_name,
                        origin="middleware",
                        source_event_seq=event.seq,
                        middleware_name=record.middleware,
                        metadata={**event.metadata, "rewrite_id": event.rewrite_id},
                    )
                )
    return MessageTape(items=_apply_rewrite_records(items, rewrite_records))


def project_tape_messages(tape: MessageTape) -> list[InferenceMessage]:
    suppressed = {
        target_id
        for item in tape.items
        if item.visibility == "internal"
        for target_id in item.metadata.get("suppresses", [])
    }
    return [
        item.to_inference_message()
        for item in tape.items
        if item.visibility == "model" and item.id not in suppressed
    ]


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
    base_items: list[TapeMessage],
    records: list[MessageRewriteRecord],
) -> list[TapeMessage]:
    items = list(base_items)
    for record in records:
        if record.operation == "replace":
            target_indexes = [
                idx for idx, item in enumerate(items) if item.id in set(record.suppresses)
            ]
            if not target_indexes:
                continue
            insert_at = min(target_indexes)
            items[insert_at:insert_at] = list(record.messages)
        else:
            insert_at = _position_index(items, record.position)
            items[insert_at:insert_at] = list(record.messages)
    return items


def apply_rewrite_records_to_tape(
    tape: MessageTape,
    records: list[MessageRewriteRecord],
) -> MessageTape:
    return MessageTape(items=_apply_rewrite_records(tape.items, records))


def _position_index(items: list[TapeMessage], position: TapePosition | None) -> int:
    if position is None:
        return len(items)
    if position.kind == "boundary":
        if position.boundary == "conversation_start":
            return 0
        return len(items)
    for idx, item in enumerate(items):
        if item.id == position.target_id:
            return idx if position.kind == "before" else idx + 1
    return len(items)
