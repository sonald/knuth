from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

import anyio

from knuth_llmd.types import ToolSpec
from knuth_toold.base import (
    ToolBase,
    ToolContext,
    ToolEffect,
    ToolResult,
    ToolRisk,
    ToolResultStatus,
)
from knuth_toold.registry import ToolRegistry


class ExecutionContextTool(ToolBase):
    def __init__(self, cwd: Path | str | None = None) -> None:
        super().__init__(default_workspace_uri=str(Path.cwd().resolve() if cwd is None else Path(cwd).resolve()))

    def _base_path(self, ctx: ToolContext) -> Path:
        return ctx.workspace_path if ctx.workspace_uri else Path(self.default_workspace_uri or ".").resolve()

    def _execution_path(self, ctx: ToolContext, raw_path: object) -> Path:
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path must be a non-empty string")
        base = self._base_path(ctx)
        path = (base / raw_path).resolve()
        if not path.is_relative_to(base):
            raise ValueError("path must stay within the execution directory")
        return path


class ReadFileTool(ExecutionContextTool):
    name: str = "read_file"
    description: str = "Read a UTF-8 text file from the current execution directory."
    parameters: dict[str, Any] = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
    }
    parallelable: bool = True
    cacheable: bool = True
    risk: ToolRisk = ToolRisk.LOW
    effect: ToolEffect = ToolEffect.READ

    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = self._execution_path(ctx, kwargs.get("path"))
        async with await anyio.open_file(path, encoding="utf-8") as file:
            content = await file.read()
        return ToolResult.success(content=content, data={"path": str(path)})


class WriteFileTool(ExecutionContextTool):
    name: str = "write_file"
    description: str = "Write UTF-8 text content to the current execution directory."
    parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
    }
    risk: ToolRisk = ToolRisk.MEDIUM
    effect: ToolEffect = ToolEffect.LOCAL_WRITE

    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = self._execution_path(ctx, kwargs.get("path"))
        content = kwargs.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        async with await anyio.open_file(path, "w", encoding="utf-8") as file:
            await file.write(content)
        base = self._base_path(ctx)
        return ToolResult.success(content=f"Wrote {path.relative_to(base)}")


class ShellTool(ExecutionContextTool):
    name: str = "shell"
    description: str = "Run a shell command in the current execution directory."
    parameters: dict[str, Any] = {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
    }
    risk: ToolRisk = ToolRisk.HIGH
    effect: ToolEffect = ToolEffect.DANGEROUS

    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty string")
        with anyio.fail_after(30):
            completed = await anyio.run_process(
                ["/bin/sh", "-c", command],
                stdin=None,
                cwd=self._base_path(ctx),
                check=False,
            )
        stdout = completed.stdout.decode()
        stderr = completed.stderr.decode().strip()
        return ToolResult(
            status=ToolResultStatus.SUCCESS if completed.returncode == 0 else ToolResultStatus.ERROR,
            content=stdout,
            error=None
            if completed.returncode == 0
            else ToolResult.from_error("process_failed", stderr or "process failed").error,
        )


class PythonTool(ExecutionContextTool):
    name: str = "python"
    description: str = "Run a Python snippet in the current execution directory."
    parameters: dict[str, Any] = {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
    }
    risk: ToolRisk = ToolRisk.HIGH
    effect: ToolEffect = ToolEffect.DANGEROUS

    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        code = kwargs.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        with anyio.fail_after(30):
            completed = await anyio.run_process(
            [sys.executable, "-c", code],
            cwd=self._base_path(ctx),
            check=False,
            )
        stdout = completed.stdout.decode()
        stderr = completed.stderr.decode().strip()
        return ToolResult(
            status=ToolResultStatus.SUCCESS if completed.returncode == 0 else ToolResultStatus.ERROR,
            content=stdout,
            error=None
            if completed.returncode == 0
            else ToolResult.from_error("process_failed", stderr or "process failed").error,
        )


class AskUserTool(ToolBase):
    name: str = "knuth.ask_user"
    description: str = "Ask the human user for clarification or confirmation."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
        "additionalProperties": False,
    }
    risk: ToolRisk = ToolRisk.LOW
    effect: ToolEffect = ToolEffect.PURE

    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        question = kwargs.get("question")
        if not isinstance(question, str) or not question:
            raise ValueError("question must be a non-empty string")
        return ToolResult.success(content=question)


def create_default_registry(cwd: Path | str | None = None) -> ToolRegistry:
    return ToolRegistry(
        [
            ReadFileTool(cwd),
            WriteFileTool(cwd),
            ShellTool(cwd),
            PythonTool(cwd),
            AskUserTool(),
        ]
    )
