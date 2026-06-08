from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import Field

from knuth.core.events import RuntimeEvent
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.types import KnuthModel
from knuth_toold import ToolBroker
from knuth_runtime.stores import EventStore


class RunContext(KnuthModel):
    run_id: str
    user_id: str | None = None
    workspace_uri: str | None = None


class ContextView(KnuthModel):
    run_id: str
    messages: list[InferenceMessage]
    tools: list[dict[str, Any]]
    diagnostics: dict[str, Any] = Field(default_factory=dict)


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
    ) -> None:
        self.event_store = event_store
        self.tool_broker = tool_broker
        self.middlewares = sorted(middlewares or [], key=lambda item: item.priority)

    async def build(self, ctx: RunContext) -> ContextView:
        events = await self.event_store.list_events(ctx.run_id)
        view = ContextView(
            run_id=ctx.run_id,
            messages=reconstruct_messages_from_events(events),
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
