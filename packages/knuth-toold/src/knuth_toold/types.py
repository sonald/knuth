from __future__ import annotations

from typing import Any, Mapping, Protocol

from knuth.core.messages import ToolCall
from knuth_llmd.types import ToolSpec
from knuth_toold.base import ToolResult


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec:
        ...

    async def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        ...


class ToolExecutor(Protocol):
    def specs(self) -> list[ToolSpec]:
        ...

    async def execute(self, call: ToolCall) -> ToolResult:
        ...
