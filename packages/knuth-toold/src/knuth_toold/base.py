from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from knuth.core.interrupts import InterruptSignal
from knuth.core.invocations import (
    ToolEffect,
    ToolInvocation,
    ToolRisk,
)
from knuth.core.tools import (
    ToolExecutionOutcome,
    ToolExecutionResult,
    ToolResult,
    ToolResultStatus,
)
from knuth.core.types import KnuthModel


class ToolManifest(KnuthModel):
    """Tool data model: what the registry, policy, and the LLM spec see."""

    name: str
    description: str
    parameters: dict[str, Any]
    parallelable: bool = False
    cacheable: bool = False
    risk: ToolRisk = ToolRisk.LOW
    effect: ToolEffect = ToolEffect.READ
    timeout_s: float | None = None
    provider: str = "builtin"

    def to_func_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolRuntimeContext:
    """Execution context handed to a tool: data now, capability handles later.

    ``interrupt_signal`` is present for local tools executing active work. A tool
    observes it at its own safe points (poll ``interrupted`` in a loop, or wake a
    blocking subprocess/network await via ``wait_interrupted``) and reports its
    own outcome; the tool never touches the RunSession or ledger directly.
    """

    run_id: str
    tool_call_id: str
    interrupt_signal: InterruptSignal | None = None


@runtime_checkable
class Tool(Protocol):
    """Tool executor: a plain object that may hold clients, sandboxes, handles."""

    @property
    def manifest(self) -> ToolManifest:
        ...

    async def invoke(
        self, invocation: ToolInvocation, ctx: ToolRuntimeContext
    ) -> ToolResult:
        ...


__all__ = [
    "InterruptSignal",
    "Tool",
    "ToolEffect",
    "ToolExecutionOutcome",
    "ToolExecutionResult",
    "ToolManifest",
    "ToolResult",
    "ToolResultStatus",
    "ToolRisk",
    "ToolRuntimeContext",
]
