from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from knuth_llmd.types import ToolCall, ToolSpec
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
