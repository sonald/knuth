"""AG-UI client tools exposed through a normal Knuth ToolProvider."""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from knuth.core.invocations import ToolInvocation
from knuth_toold import (
    ToolEffect,
    ToolManifest,
    ToolResult,
    ToolRisk,
    ToolRuntimeContext,
)

_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,80}$")
_EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}}


class AGUIClientToolProvider:
    """ToolProvider for browser/client-side AG-UI tools.

    The runtime can propose these tools and wait for an external result, but it
    must never execute them in-process.
    """

    name = "agui-client"

    def __init__(self, manifests: list[ToolManifest] | None = None) -> None:
        self._manifests: dict[str, ToolManifest] = {}
        self._lock = threading.RLock()
        self.register_many(manifests or [])

    @property
    def has_tools(self) -> bool:
        with self._lock:
            return bool(self._manifests)

    async def list_tools(self) -> list[ToolManifest]:
        with self._lock:
            return list(self._manifests.values())

    async def call_tool(
        self, invocation: ToolInvocation, _ctx: ToolRuntimeContext
    ) -> ToolResult:
        return ToolResult.from_error(
            "client_tool_not_executable_on_server",
            f"Client tool {invocation.tool_name} must be executed by the AG-UI client",
            retryable=False,
        )

    async def awaits_external_result(self, _invocation: ToolInvocation) -> bool:
        return True

    def register_agui_tools(self, tools: Any) -> None:
        self.register_many(manifests_from_agui(tools))

    def register_many(self, manifests: list[ToolManifest]) -> None:
        with self._lock:
            for manifest in manifests:
                self._register(manifest)

    def _register(self, manifest: ToolManifest) -> None:
        manifest = manifest.model_copy(update={"provider": self.name})
        existing = self._manifests.get(manifest.name)
        if existing is not None and _fingerprint(existing) != _fingerprint(manifest):
            raise ValueError(
                f"client tool already registered differently: {manifest.name}"
            )
        self._manifests[manifest.name] = manifest


def create_agui_client_tool_provider() -> AGUIClientToolProvider:
    return AGUIClientToolProvider()


def manifests_from_agui(tools: Any) -> list[ToolManifest]:
    if tools in (None, ""):
        return []
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
    return manifests


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
        provider=AGUIClientToolProvider.name,
    )


def _fingerprint(manifest: ToolManifest) -> str:
    return json.dumps(
        manifest.model_dump(mode="json", exclude={"provider"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
