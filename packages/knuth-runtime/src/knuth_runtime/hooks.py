from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from knuth.core.types import KnuthModel


class HookAction(StrEnum):
    CONTINUE = "continue"
    PAUSE = "pause"
    TERMINATE = "terminate"
    MUTATE = "mutate"


class HookResult(KnuthModel):
    action: HookAction = HookAction.CONTINUE
    reason: str | None = None
    patch: dict[str, Any] = Field(default_factory=dict)


class HookContext(KnuthModel):
    run_id: str
    namespace: str
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HookHandler(Protocol):
    async def __call__(self, ctx: HookContext) -> HookResult:
        ...


class HookRegistration(KnuthModel):
    namespace: str
    name: str
    handler_id: str
    priority: int = 100
    blocking: bool = False
    timeout_s: float | None = None


class HookManager:
    def __init__(self) -> None:
        self._blocking: list[tuple[HookRegistration, HookHandler]] = []
        self._observers: list[tuple[HookRegistration, HookHandler]] = []

    def register(self, registration: HookRegistration, handler: HookHandler) -> None:
        target = self._blocking if registration.blocking else self._observers
        target.append((registration, handler))
        target.sort(key=lambda item: item[0].priority)

    async def dispatch_blocking(self, ctx: HookContext) -> HookResult:
        for _, handler in self._matching(self._blocking, ctx):
            result = await handler(ctx)
            if result.action != HookAction.CONTINUE:
                return result
        return HookResult()

    async def emit_observer(self, ctx: HookContext) -> None:
        for _, handler in self._matching(self._observers, ctx):
            try:
                await handler(ctx)
            except Exception:
                pass

    def _matching(
        self,
        handlers: list[tuple[HookRegistration, HookHandler]],
        ctx: HookContext,
    ) -> list[tuple[HookRegistration, HookHandler]]:
        return [
            item
            for item in handlers
            if item[0].namespace == ctx.namespace and item[0].name == ctx.name
        ]
