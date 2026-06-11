from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import anyio

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import ToolResult, ToolResultStatus
from knuth_toold.base import ToolManifest, ToolRuntimeContext

from knuth_cli.tools.files import _ExecutionContextTool
from knuth_cli.tools.process_output import render_tagged_process_output


class ShellTool(_ExecutionContextTool):
    def __init__(
        self,
        cwd: Path | str | None = None,
        *,
        offload_root: Path | str | None = None,
        threshold_bytes: int = 4096,
        preview_bytes: int = 2048,
    ) -> None:
        super().__init__(cwd)
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
                "Run a shell command in the current execution directory and return "
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
            cwd=self._base_path(),
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
            "cwd": str(self._base_path()),
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
