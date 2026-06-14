"""Per-request AG-UI client tools exposed as Knuth invocation overlays."""

from __future__ import annotations

import re
from typing import Any

from knuth.core.invocations import ToolInvocation
from knuth_toold import (
    ToolEffect,
    ToolExecutionMode,
    ToolManifest,
    ToolResult,
    ToolRisk,
    ToolRuntimeContext,
)

_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,80}$")
_EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}}


class ClientToolProvider:
    """ToolProvider for browser/client-side AG-UI tools.

    The runtime can propose these tools and wait for an external result, but it
    must never execute them in-process.
    """

    name = "agui-client"

    def __init__(self, manifests: list[ToolManifest]) -> None:
        self._manifests = tuple(manifests)

    @property
    def has_tools(self) -> bool:
        return bool(self._manifests)

    async def list_tools(self) -> list[ToolManifest]:
        return list(self._manifests)

    async def call_tool(
        self, invocation: ToolInvocation, _ctx: ToolRuntimeContext
    ) -> ToolResult:
        return ToolResult.from_error(
            "client_tool_not_executable_on_server",
            f"Client tool {invocation.tool_name} must be executed by the AG-UI client",
            retryable=False,
        )


def client_tool_provider_from_agui(tools: Any) -> ClientToolProvider:
    if tools in (None, ""):
        return ClientToolProvider([])
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")

    manifests: list[ToolManifest] = []
    names: set[str] = set()
    for item in tools:
        manifest = _manifest_from_agui_tool(item)
        if manifest.name in names:
            raise ValueError(f"duplicate client tool name: {manifest.name}")
        names.add(manifest.name)
        manifests.append(manifest)
    return ClientToolProvider(manifests)


def _manifest_from_agui_tool(item: Any) -> ToolManifest:
    if not isinstance(item, dict):
        raise ValueError("each tool must be an object")
    tool_type = item.get("type", "function")
    if tool_type != "function":
        raise ValueError(f"unsupported client tool type: {tool_type}")

    function = item.get("function")
    if function is None:
        function = item
    if not isinstance(function, dict):
        raise ValueError("function tool must include a function object")

    name = function.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ValueError("client tool name must match [A-Za-z_][A-Za-z0-9_]{0,80}")

    description = function.get("description")
    parameters = function.get("parameters") or _EMPTY_OBJECT_SCHEMA
    if not isinstance(description, str):
        description = ""
    if not isinstance(parameters, dict):
        raise ValueError(f"client tool {name} parameters must be an object schema")

    return ToolManifest(
        name=name,
        description=description,
        parameters=parameters,
        effect=ToolEffect.READ,
        risk=ToolRisk.LOW,
        execution_mode=ToolExecutionMode.EXTERNAL,
        provider=ClientToolProvider.name,
    )
