from __future__ import annotations

from pathlib import Path

import anyio

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import ToolResult
from knuth_toold.base import ToolManifest, ToolRuntimeContext


class _ExecutionContextTool:
    def __init__(self, cwd: Path | str | None = None) -> None:
        path = Path.cwd() if cwd is None else Path(cwd)
        self._execution_dir = path.resolve()

    def _base_path(self) -> Path:
        return self._execution_dir

    def _execution_path(self, raw_path: object) -> Path:
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path must be a non-empty string")
        base = self._base_path()
        path = (base / raw_path).resolve()
        if not path.is_relative_to(base):
            raise ValueError("path must stay within the execution directory")
        return path


class ReadFileTool(_ExecutionContextTool):
    max_read_bytes = 32 * 1024

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the current execution directory "
                "with line numbers. Path must stay within the execution "
                "directory. Reads support 1-based offset and line limit. "
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

        path = self._execution_path(raw_path)
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

        relative_path = path.relative_to(self._base_path())
        end_line = offset + len(selected) - 1
        header = (
            f"File({relative_path}) - Lines {offset}-{end_line} "
            f"of {len(lines)} total:"
        )
        return ToolResult.success(content="\n".join([header, *rendered_lines]))


class WriteFileTool(_ExecutionContextTool):
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="write_file",
            description=(
                "Write UTF-8 text content to a file inside the current execution "
                "directory. Parent directories are created as needed."
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
        path = self._execution_path(invocation.args.get("path"))
        content = invocation.args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        async with await anyio.open_file(path, "w", encoding="utf-8") as file:
            await file.write(content)
        return ToolResult.success(
            content=f"Wrote {path.relative_to(self._base_path())}"
        )


class EditFileTool(_ExecutionContextTool):
    _encodings = (
        "utf-8-sig",
        "utf-8",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "gb18030",
    )

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="edit_file",
            description=(
                "Edit a text file inside the current execution directory by exact "
                "string replacement. old_string must be non-empty and different "
                "from new_string. By default the match must be unique; set "
                "replace_all=true to replace every match. The tool detects common "
                "text encodings and writes the file back using the original encoding."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            risk=ToolRisk.MEDIUM,
            effect=ToolEffect.LOCAL_WRITE,
        )

    async def invoke(
        self, invocation: ToolInvocation, ctx: ToolRuntimeContext
    ) -> ToolResult:
        _ = ctx
        path = self._execution_path(invocation.args.get("path"))
        if not path.exists() or not path.is_file():
            raise ValueError("path must point to an existing file")

        old_string = invocation.args.get("old_string")
        new_string = invocation.args.get("new_string")
        replace_all = bool(invocation.args.get("replace_all") or False)
        if not isinstance(old_string, str) or old_string == "":
            raise ValueError("old_string must be a non-empty string")
        if not isinstance(new_string, str):
            raise ValueError("new_string must be a string")
        if old_string == new_string:
            raise ValueError("new_string must be different from old_string")

        raw_content = await anyio.Path(path).read_bytes()
        text, encoding = self._decode_text(raw_content)
        count = text.count(old_string)
        if count == 0:
            raise ValueError("old_string was not found")
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string found {count} matches; set replace_all=true to replace all"
            )

        replacement_count = count if replace_all else 1
        edited = text.replace(old_string, new_string, -1 if replace_all else 1)
        await anyio.Path(path).write_bytes(edited.encode(encoding))
        relative_path = path.relative_to(self._base_path())
        return ToolResult.success(
            content=(
                f"Edited {relative_path} "
                f"(replacements={replacement_count}, encoding={encoding})"
            )
        )

    def _decode_text(self, raw_content: bytes) -> tuple[str, str]:
        if b"\x00" in raw_content[:4096]:
            unicode_candidates = ("utf-16", "utf-16-le", "utf-16-be")
        else:
            unicode_candidates = self._encodings

        for encoding in unicode_candidates:
            try:
                text = raw_content.decode(encoding)
                if text.encode(encoding) == raw_content:
                    return text, encoding
            except UnicodeError:
                continue
        raise ValueError(
            "file is not a supported text encoding; supported encodings are "
            + ", ".join(self._encodings)
        )
