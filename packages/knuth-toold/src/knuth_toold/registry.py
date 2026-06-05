from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points
from typing import Any

from knuth_toold.base import ToolBase, ToolContext, ToolManifest, ToolResult
from knuth_toold.providers import ToolProvider


class BuiltinToolProvider:
    name = "builtin"

    def __init__(self, tools: Iterable[ToolBase] = ()) -> None:
        self._tools: dict[str, ToolBase] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolBase) -> None:
        self._tools[tool.name] = tool

    async def list_tools(self) -> list[ToolManifest]:
        return [tool.manifest() for tool in self._tools.values()]

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        return await self._tools[name](ctx, **args)


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolBase] = ()) -> None:
        self._providers: dict[str, ToolProvider] = {}
        self._manifest_index: dict[str, tuple[ToolManifest, str]] = {}
        self._builtin = BuiltinToolProvider()
        self.add_provider(self._builtin)
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolBase) -> None:
        self._builtin.register(tool)
        self._manifest_index.clear()

    def add_provider(self, provider: ToolProvider) -> None:
        self._providers[provider.name] = provider
        self._manifest_index.clear()

    async def refresh(self) -> None:
        self._manifest_index.clear()
        for provider_name, provider in self._providers.items():
            for manifest in await provider.list_tools():
                self._manifest_index[manifest.name] = (
                    manifest.model_copy(update={"provider": provider_name}),
                    provider_name,
                )

    def get_manifest(self, name: str) -> ToolManifest:
        return self._manifest_index[name][0]

    def get_provider_for_tool(self, name: str) -> ToolProvider:
        return self._providers[self._manifest_index[name][1]]

    def list_visible_manifests(self) -> list[ToolManifest]:
        return [item[0] for item in self._manifest_index.values()]

    async def discover_entry_points(self, group: str = "knuth.tools") -> None:
        for entry_point in entry_points(group=group):
            factory = entry_point.load()
            self.add_provider(factory())
        await self.refresh()
