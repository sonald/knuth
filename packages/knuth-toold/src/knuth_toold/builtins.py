from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import anyio

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import ToolResult, ToolResultStatus

from knuth_toold.base import ToolManifest, ToolRuntimeContext
from knuth_toold.process_output import render_tagged_process_output
from knuth_toold.registry import ToolRegistry


# Tools take paths as given: absolute paths are used as-is, relative paths
# resolve against the process cwd (plain OS semantics). Path access control
# belongs to the policy layer, not here (ADR-005).
def _require_path(raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("path must be a non-empty string")
    return Path(raw_path)


class ReadFileTool:
    max_read_bytes = 32 * 1024

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="read_file",
            description=(
                "Read a UTF-8 text file with line numbers. Paths may be "
                "absolute or relative to the process working directory. "
                "Reads support 1-based offset and line limit. "
                "Maximum returned content per call is 32KiB (32768 bytes); "
                "larger requests fail with no partial content returned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "default": 1},
                    "limit": {"type": "integer", "default": 200},
                },
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
        _ = ctx
        raw_path = invocation.args.get("path")
        offset = int(invocation.args.get("offset") or 1)
        limit = int(invocation.args.get("limit") or 200)
        if offset < 1:
            raise ValueError("offset must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        path = _require_path(raw_path)
        async with await anyio.open_file(path, encoding="utf-8") as file:
            lines = await file.readlines()

        selected = lines[offset - 1 : offset - 1 + limit]
        accumulated_bytes = 0
        rendered_lines: list[str] = []
        for index, line in enumerate(selected, start=offset):
            line_text = line.rstrip("\n\r")
            line_bytes = len(line.encode("utf-8"))
            if line_bytes > self.max_read_bytes:
                raise ValueError(
                    f"Line {index} is {line_bytes} bytes, exceeding read_file "
                    f"max of {self.max_read_bytes} bytes; no content returned"
                )
            accumulated_bytes += line_bytes
            if accumulated_bytes > self.max_read_bytes:
                raise ValueError(
                    "Requested content exceeds read_file max of "
                    f"{self.max_read_bytes} bytes ({accumulated_bytes} bytes "
                    "needed); no content returned"
                )
            rendered_lines.append(f"{index:4d}: {line_text}")

        if not selected:
            return ToolResult.success(
                content=(
                    "No content found in the specified range "
                    f"(file has {len(lines)} total lines)"
                )
            )

        end_line = offset + len(selected) - 1
        header = (
            f"File({path}) - Lines {offset}-{end_line} "
            f"of {len(lines)} total:"
        )
        return ToolResult.success(content="\n".join([header, *rendered_lines]))


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
        _ = ctx
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


class ShellTool:
    def __init__(
        self,
        *,
        offload_root: Path | str | None = None,
        threshold_bytes: int = 4096,
        preview_bytes: int = 2048,
    ) -> None:
        self._offload_root = (
            Path.home() / ".knuth" / "offload" / "shell"
            if offload_root is None
            else Path(offload_root)
        )
        self._threshold_bytes = threshold_bytes
        self._preview_bytes = preview_bytes

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="shell",
            description=(
                "Run a shell command in the process working directory and return "
                "structured stdout, stderr, return_code, and offload metadata when "
                "output is too large."
            ),
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
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        completed = await anyio.run_process(
            ["/bin/sh", "-c", command],
            check=False,
        )
        stdout_bytes = completed.stdout
        stderr_bytes = completed.stderr
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        offload = await self._build_offload_payload(
            invocation=invocation,
            ctx=ctx,
            command=command,
            return_code=completed.returncode,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
        )
        if offload["status"] == "offloaded":
            stdout = self._preview(stdout_bytes)
            stderr = self._preview(stderr_bytes)
        content = render_tagged_process_output(
            stdout=stdout,
            stderr=stderr,
            return_code=completed.returncode,
            offload=offload,
        )
        status = (
            ToolResultStatus.SUCCESS
            if completed.returncode == 0
            else ToolResultStatus.ERROR
        )
        return ToolResult(
            status=status,
            content=content,
            error=None
            if completed.returncode == 0
            else ToolResult.from_error(
                "process_failed",
                stderr or f"process failed with return code {completed.returncode}",
                retryable=True,
            ).error,
        )

    async def _build_offload_payload(
        self,
        *,
        invocation: ToolInvocation,
        ctx: ToolRuntimeContext,
        command: str,
        return_code: int,
        stdout_bytes: bytes,
        stderr_bytes: bytes,
    ) -> dict:
        stdout_size = len(stdout_bytes)
        stderr_size = len(stderr_bytes)
        if (
            stdout_size <= self._threshold_bytes
            and stderr_size <= self._threshold_bytes
        ):
            return {
                "status": "inline",
                "threshold_bytes": self._threshold_bytes,
                "preview_bytes": self._preview_bytes,
            }

        run_id = ctx.run_id or invocation.run_id
        tool_call_id = ctx.tool_call_id or invocation.tool_call_id
        offload_dir = self._offload_root / run_id / tool_call_id
        await anyio.Path(offload_dir).mkdir(parents=True, exist_ok=True)
        stdout_path = offload_dir / "stdout.txt"
        stderr_path = offload_dir / "stderr.txt"
        result_path = offload_dir / "result.json"
        await anyio.Path(stdout_path).write_bytes(stdout_bytes)
        await anyio.Path(stderr_path).write_bytes(stderr_bytes)

        metadata = {
            "version": 1,
            "tool": "shell",
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "cwd": str(Path.cwd()),
            "return_code": return_code,
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "threshold_bytes": self._threshold_bytes,
            "preview_bytes": self._preview_bytes,
            "stdout": self._file_metadata(stdout_path, stdout_bytes),
            "stderr": self._file_metadata(stderr_path, stderr_bytes),
        }
        await anyio.Path(result_path).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "status": "offloaded",
            "message": "Full output saved for inspection.",
            "result_path": str(result_path),
            "threshold_bytes": self._threshold_bytes,
            "preview_bytes": self._preview_bytes,
            "stdout": metadata["stdout"],
            "stderr": metadata["stderr"],
        }

    def _preview(self, content: bytes) -> str:
        return content[: self._preview_bytes].decode(errors="replace")

    def _file_metadata(self, path: Path, content: bytes) -> dict:
        return {
            "path": str(path),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }


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


class BuiltinToolProvider:
    name = "builtin"

    def __init__(self) -> None:
        tools = (
            ReadFileTool(),
            WriteFileTool(),
            ShellTool(),
            PythonTool(),
        )
        self._tools = {tool.manifest.name: tool for tool in tools}

    async def list_tools(self) -> list[ToolManifest]:
        return [tool.manifest for tool in self._tools.values()]

    async def call_tool(
        self,
        invocation: ToolInvocation,
        ctx: ToolRuntimeContext,
    ) -> ToolResult:
        return await self._tools[invocation.tool_name].invoke(invocation, ctx)


def create_default_registry(
    *,
    enable_entry_point_discovery: bool = False,
) -> ToolRegistry:
    registry = ToolRegistry(
        enable_entry_point_discovery=enable_entry_point_discovery,
    )
    registry.add_provider(BuiltinToolProvider())
    return registry
