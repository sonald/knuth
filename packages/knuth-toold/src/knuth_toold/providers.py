from __future__ import annotations

from typing import Any, Protocol

from knuth_toold.base import ToolContext, ToolManifest, ToolResult


class ToolProvider(Protocol):
    name: str

    async def list_tools(self) -> list[ToolManifest]:
        ...

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        ...
