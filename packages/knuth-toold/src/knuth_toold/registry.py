from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points

from knuth.core.invocations import ToolInvocation
from knuth.core.tools import ToolResult

from knuth_toold.base import Tool, ToolManifest, ToolRuntimeContext
from knuth_toold.providers import ToolProvider


class BuiltinToolProvider:
    name = "builtin"

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.manifest.name] = tool

    async def list_tools(self) -> list[ToolManifest]:
        return [tool.manifest for tool in self._tools.values()]

    async def call_tool(
        self,
        invocation: ToolInvocation,
        ctx: ToolRuntimeContext,
    ) -> ToolResult:
        return await self._tools[invocation.tool_name].invoke(invocation, ctx)


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        enable_entry_point_discovery: bool = False,
    ) -> None:
        self._providers: dict[str, ToolProvider] = {}
        self._manifest_index: dict[str, tuple[ToolManifest, str]] = {}
        self._builtin = BuiltinToolProvider()
        # Entry-point plugins execute third-party code in-process; they stay
        # off unless the host explicitly opted in (--enable-plugins).
        self._enable_entry_point_discovery = enable_entry_point_discovery
        self._entry_points_discovered = False
        self.add_provider(self._builtin)
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._builtin.register(tool)
        self._manifest_index.clear()

    def add_provider(self, provider: ToolProvider) -> None:
        self._providers[provider.name] = provider
        self._manifest_index.clear()

    async def refresh(self) -> None:
        if self._enable_entry_point_discovery and not self._entry_points_discovered:
            self._discover_entry_points()
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

    def _discover_entry_points(self, group: str = "knuth.tools") -> None:
        for entry_point in entry_points(group=group):
            factory = entry_point.load()
            self.add_provider(factory())
        self._entry_points_discovered = True
