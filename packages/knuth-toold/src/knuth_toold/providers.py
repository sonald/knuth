from __future__ import annotations

from typing import Protocol

from knuth.core.invocations import ToolInvocation
from knuth.core.tools import ToolExecutionResult, ToolResult

from knuth_toold.base import ToolManifest, ToolRuntimeContext


class ToolProvider(Protocol):
    name: str

    async def list_tools(self) -> list[ToolManifest]:
        ...

    async def call_tool(
        self,
        invocation: ToolInvocation,
        ctx: ToolRuntimeContext,
    ) -> ToolResult | ToolExecutionResult:
        ...
