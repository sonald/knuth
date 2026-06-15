from __future__ import annotations

from pathlib import Path

import anyio

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import ToolResult
from knuth_toold.base import ToolManifest, ToolRuntimeContext


# Tools take paths as given: absolute paths are used as-is, relative paths
# resolve against the process cwd (plain OS semantics). Path access control
# belongs to the policy layer, not here (ADR-005).
def _require_path(raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("path must be a non-empty string")
    return Path(raw_path)


class EditFileTool:
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
                "Edit a text file by exact string replacement. Paths may be "
                "absolute or relative to the process working directory. "
                "old_string must be non-empty and different "
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
        path = _require_path(invocation.args.get("path"))
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
        return ToolResult.success(
            content=(
                f"Edited {path} "
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
