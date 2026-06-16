from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
import signal as signalmod
import sys
from pathlib import Path
from typing import Any

import anyio

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import (
    ToolExecutionResult,
    ToolResult,
    ToolResultStatus,
)

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
        interrupt_grace_s: float = 2.0,
    ) -> None:
        self._offload_root = (
            Path.home() / ".knuth" / "offload" / "shell"
            if offload_root is None
            else Path(offload_root)
        )
        self._threshold_bytes = threshold_bytes
        self._preview_bytes = preview_bytes
        # Grace after a gentle terminate before a force kill on user stop.
        self._interrupt_grace_s = interrupt_grace_s

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
    ) -> ToolResult | ToolExecutionResult:
        command = invocation.args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")

        stdout_bytes, stderr_bytes, return_code, interrupted = await self._run_command(
            command, ctx.interrupt_signal
        )
        if interrupted:
            # Cooperative stop: report INTERRUPTED with whatever partial output
            # was captured and an explicit warning about possible side effects.
            return self._interrupted_result(stdout_bytes, stderr_bytes, return_code)

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        offload = await self._build_offload_payload(
            invocation=invocation,
            ctx=ctx,
            command=command,
            return_code=return_code,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
        )
        if offload["status"] == "offloaded":
            stdout = self._preview(stdout_bytes)
            stderr = self._preview(stderr_bytes)
        content = render_tagged_process_output(
            stdout=stdout,
            stderr=stderr,
            return_code=return_code,
            offload=offload,
        )
        status = (
            ToolResultStatus.SUCCESS
            if return_code == 0
            else ToolResultStatus.ERROR
        )
        return ToolResult(
            status=status,
            content=content,
            error=None
            if return_code == 0
            else ToolResult.from_error(
                "process_failed",
                stderr or f"process failed with return code {return_code}",
                retryable=True,
            ).error,
        )

    async def _run_command(
        self, command: str, signal: Any | None
    ) -> tuple[bytes, bytes, int, bool]:
        """Run the command, cooperating with an interrupt signal.

        Returns ``(stdout, stderr, return_code, interrupted)``. On interrupt the
        child is sent a gentle terminate, then force-killed after a short grace;
        whatever output was captured before then is still returned.
        """
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        interrupted = False
        wakeable = signal is not None and hasattr(signal, "wait_interrupted")
        # New session so a gentle stop reaches the whole command's process
        # group, not just /bin/sh — otherwise a grandchild (e.g. ``sleep``)
        # keeps the output pipe open and the drain never sees EOF.
        process = await anyio.open_process(
            ["/bin/sh", "-c", command], stdin=None, start_new_session=True
        )
        try:
            async with anyio.create_task_group() as outer:
                if wakeable:

                    async def _watch() -> None:
                        nonlocal interrupted
                        await signal.wait_interrupted()
                        interrupted = True
                        await self._terminate(process)

                    outer.start_soon(_watch)

                # Drain both pipes until EOF (process exit or termination).
                async with anyio.create_task_group() as drains:
                    drains.start_soon(self._drain, process.stdout, stdout_buf)
                    drains.start_soon(self._drain, process.stderr, stderr_buf)
                await process.wait()
                outer.cancel_scope.cancel()
        finally:
            if process.returncode is None:
                with anyio.CancelScope(shield=True):
                    self._signal_group(process, signalmod.SIGKILL)
                    await process.wait()
        return (
            bytes(stdout_buf),
            bytes(stderr_buf),
            process.returncode if process.returncode is not None else -1,
            interrupted,
        )

    @staticmethod
    async def _drain(stream: Any | None, buffer: bytearray) -> None:
        if stream is None:
            return
        async for chunk in stream:
            buffer.extend(chunk)

    async def _terminate(self, process: Any) -> None:
        self._signal_group(process, signalmod.SIGTERM)
        await anyio.sleep(self._interrupt_grace_s)
        if process.returncode is None:
            self._signal_group(process, signalmod.SIGKILL)

    @staticmethod
    def _signal_group(process: Any, sig: int) -> None:
        """Signal the command's whole process group, falling back to the leader."""
        pid = getattr(process, "pid", None)
        if pid is None:
            return
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate() if sig == signalmod.SIGTERM else process.kill()
            except ProcessLookupError:
                pass

    def _interrupted_result(
        self, stdout_bytes: bytes, stderr_bytes: bytes, return_code: int
    ) -> ToolExecutionResult:
        stdout = self._preview(stdout_bytes)
        stderr = self._preview(stderr_bytes)
        observation = (
            "Command was interrupted by the user before it finished. The partial"
            " output below may be incomplete, and the command may already have"
            " produced side effects.\n"
            + render_tagged_process_output(
                stdout=stdout,
                stderr=stderr,
                return_code=return_code,
                offload={"status": "interrupted"},
            )
        )
        return ToolExecutionResult.interrupted(observation, tool_status="interrupted")

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
