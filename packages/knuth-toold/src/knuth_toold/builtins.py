from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anyio

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import ToolResult, ToolResultStatus

from knuth_toold.base import ToolManifest, ToolRuntimeContext
from knuth_toold.registry import ToolRegistry


# Tools take paths as given: absolute paths are used as-is, relative paths
# resolve against the process cwd (plain OS semantics). Path access control
# belongs to the policy layer, not here (ADR-005).
def _require_path(raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("path must be a non-empty string")
    return Path(raw_path)


class ReadFileTool:
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="read_file",
            description=(
                "Read a UTF-8 text file. Paths may be absolute or relative "
                "to the process working directory."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            parallelable=True,
            cacheable=True,
            risk=ToolRisk.LOW,
            effect=ToolEffect.READ,
        )

    async def invoke(
        self, invocation: ToolInvocation, ctx: ToolRuntimeContext
    ) -> ToolResult:
        path = _require_path(invocation.args.get("path"))
        async with await anyio.open_file(path, encoding="utf-8") as file:
            content = await file.read()
        return ToolResult.success(content=content, data={"path": str(path)})


class WriteFileTool:
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="write_file",
            description=(
                "Write UTF-8 text content to a file. Paths may be absolute "
                "or relative to the process working directory. Parent "
                "directories are created as needed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            risk=ToolRisk.MEDIUM,
            effect=ToolEffect.LOCAL_WRITE,
        )

    async def invoke(
        self, invocation: ToolInvocation, ctx: ToolRuntimeContext
    ) -> ToolResult:
        path = _require_path(invocation.args.get("path"))
        content = invocation.args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        async with await anyio.open_file(path, "w", encoding="utf-8") as file:
            await file.write(content)
        return ToolResult.success(content=f"Wrote {path}")


class _SubprocessTool:
    async def _run(self, command: list[str], timeout_s: float) -> ToolResult:
        with anyio.fail_after(timeout_s):
            completed = await anyio.run_process(
                command,
                stdin=None,
                check=False,
            )
        stdout = completed.stdout.decode()
        stderr = completed.stderr.decode().strip()
        return ToolResult(
            status=ToolResultStatus.SUCCESS
            if completed.returncode == 0
            else ToolResultStatus.ERROR,
            content=stdout,
            error=None
            if completed.returncode == 0
            else ToolResult.from_error("process_failed", stderr or "process failed").error,
        )


class ShellTool(_SubprocessTool):
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="shell",
            description="Run a shell command in the process working directory.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            risk=ToolRisk.HIGH,
            effect=ToolEffect.DANGEROUS,
            timeout_s=30.0,
        )

    async def invoke(
        self, invocation: ToolInvocation, ctx: ToolRuntimeContext
    ) -> ToolResult:
        command = invocation.args.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty string")
        return await self._run(["/bin/sh", "-c", command], timeout_s=30)


class PythonTool(_SubprocessTool):
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="python",
            description="Run a Python snippet in the process working directory.",
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
                "additionalProperties": False,
            },
            risk=ToolRisk.HIGH,
            effect=ToolEffect.DANGEROUS,
            timeout_s=30.0,
        )

    async def invoke(
        self, invocation: ToolInvocation, ctx: ToolRuntimeContext
    ) -> ToolResult:
        code = invocation.args.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        return await self._run([sys.executable, "-c", code], timeout_s=30)


def create_default_registry(
    *,
    enable_entry_point_discovery: bool = False,
) -> ToolRegistry:
    return ToolRegistry(
        [
            ReadFileTool(),
            WriteFileTool(),
            ShellTool(),
            PythonTool(),
        ],
        enable_entry_point_discovery=enable_entry_point_discovery,
    )
