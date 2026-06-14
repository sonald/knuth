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
from knuth.core.runtime_events import ContextSnapshot
from knuth.core.types import KnuthModel
from knuth_toold import ToolBroker, ToolProvider

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


class MessageMiddleware(ABC):
    """Full-power view rewriter. Core-system use only — third parties extend
    context through ``SystemSectionProvider``, never through middleware."""

    name: str
    priority: int = 100

    @abstractmethod
    async def process(self, ctx: RunContext, view: ContextView) -> ContextView:
        ...


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ContextBuilder:
    """Builds the model-facing view through fixed stages:

    assemble -> redact -> compact -> tool_filter -> freeze

    The stage order is load-bearing: redaction runs before compaction so no
    summarizer ever sees a secret, and freeze computes the ContextSnapshot
    after which the view must not change.
    """

    def __init__(
        self,
        ledger: RunLedger,
        tool_broker: ToolBroker,
        middlewares: list[MessageMiddleware] | None = None,
        section_providers: list[SystemSectionProvider] | None = None,
        redactor: ContextRedactor | None = None,
    ) -> None:
        self.ledger = ledger
        self.tool_broker = tool_broker
        self.middlewares = sorted(middlewares or [], key=lambda item: item.priority)
        self.section_providers = list(section_providers or [])
        self.redactor = redactor

    async def build(
        self,
        ctx: RunContext,
        *,
        model_config_fingerprint: str = "",
        tool_providers: tuple[ToolProvider, ...] = (),
    ) -> ContextView:
        view = await self._assemble(ctx)
        if self.redactor is not None:
            view = await self.redactor.redact(ctx, view)
        view = await self._compact(ctx, view)
        view = await self._tool_filter(ctx, view, tool_providers=tool_providers)
        return self._freeze(view, model_config_fingerprint)

    async def _assemble(self, ctx: RunContext) -> ContextView:
        events = await self.ledger.list_events(ctx.run_id)
        messages = await reconstruct_messages_from_events(
            events, self.ledger.get_artifact_text
        )
        preamble = await self._preamble(ctx)
        if preamble:
            messages.insert(
                0,
                InferenceMessage(role=InferenceRole.SYSTEM, content=preamble),
            )
        return ContextView(run_id=ctx.run_id, messages=messages, tools=[])

    async def _preamble(self, ctx: RunContext) -> str | None:
        sections: list[SystemSection] = []
        for provider in self.section_providers:
            sections.extend(await provider.sections(ctx))
        return assemble_preamble(sections)

    async def _compact(self, ctx: RunContext, view: ContextView) -> ContextView:
        for middleware in self.middlewares:
            view = await middleware.process(ctx, view)
        return view

    async def _tool_filter(
        self,
        ctx: RunContext,
        view: ContextView,
        *,
        tool_providers: tuple[ToolProvider, ...] = (),
    ) -> ContextView:
        tools = await self.tool_broker.list_visible_tools(
            ctx.run_id, overlay_providers=tool_providers
        )
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
    """Conversation fold: a closed, typed mapping from decision events to the
    message sequence. Aggregate invariants guarantee the result is always a
    provider-valid sequence, so no defensive repair happens here."""
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
        elif event.type == "verification.failed":
            messages.append(
                InferenceMessage(role=InferenceRole.USER, content=event.feedback)
            )
    return messages
