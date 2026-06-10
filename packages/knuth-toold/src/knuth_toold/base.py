from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import ToolResult, ToolResultStatus
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
    """Execution context handed to a tool: data now, capability handles later."""

    run_id: str
    tool_call_id: str
    workspace_uri: str | None = None
    idempotency_key: str | None = None

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace_uri or ".").resolve()


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
    "Tool",
    "ToolEffect",
    "ToolManifest",
    "ToolResult",
    "ToolResultStatus",
    "ToolRisk",
    "ToolRuntimeContext",
]
