from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import Field

from knuth.core.events import RuntimeEvent
from knuth.core.messages import (
    InferenceMessage,
    InferenceRole,
    SystemSection,
    SystemSectionSource,
)
from knuth.core.types import KnuthModel
from knuth_toold import ToolBroker
from knuth_runtime.stores import EventStore

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
    user_id: str | None = None
    workspace_uri: str | None = None


class ContextView(KnuthModel):
    run_id: str
    messages: list[InferenceMessage]
    tools: list[dict[str, Any]]
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


class MessageMiddleware(ABC):
    name: str
    priority: int = 100

    @abstractmethod
    async def process(self, ctx: RunContext, view: ContextView) -> ContextView:
        ...


class ContextBuilder:
    def __init__(
        self,
        event_store: EventStore,
        tool_broker: ToolBroker,
        middlewares: list[MessageMiddleware] | None = None,
        section_providers: list[SystemSectionProvider] | None = None,
    ) -> None:
        self.event_store = event_store
        self.tool_broker = tool_broker
        self.middlewares = sorted(middlewares or [], key=lambda item: item.priority)
        self.section_providers = list(section_providers or [])

    async def _preamble(self, ctx: RunContext) -> str | None:
        sections: list[SystemSection] = []
        for provider in self.section_providers:
            sections.extend(await provider.sections(ctx))
        return assemble_preamble(sections)

    async def build(self, ctx: RunContext) -> ContextView:
        events = await self.event_store.list_events(ctx.run_id)
        messages = reconstruct_messages_from_events(events)
        preamble = await self._preamble(ctx)
        if preamble:
            messages.insert(
                0,
                InferenceMessage(role=InferenceRole.SYSTEM, content=preamble),
            )
        view = ContextView(
            run_id=ctx.run_id,
            messages=messages,
            tools=await self.tool_broker.list_visible_tools(ctx.run_id),
        )
        for middleware in self.middlewares:
            view = await middleware.process(ctx, view)
        return view


def reconstruct_messages_from_events(events: list[RuntimeEvent]) -> list[InferenceMessage]:
    messages: list[InferenceMessage] = []
    for event in events:
        if event.type == "user.message":
            messages.append(
                InferenceMessage(
                    role=InferenceRole.USER,
                    content=event.content,
                )
            )
        elif event.type == "model.completed":
            messages.append(event.message)
        elif event.type == "tool.completed":
            messages.append(event.message)
    return messages
