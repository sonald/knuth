from __future__ import annotations

import glob as globlib
import json
from pathlib import Path
import re

import anyio

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.tools import ToolResult, ToolResultStatus
from knuth_toold.base import ToolManifest, ToolRuntimeContext


_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000
_TYPE_GLOBS = {
    "js": "*.js",
    "jsx": "*.jsx",
    "md": "*.md",
    "py": "*.py",
    "rust": "*.rs",
    "rs": "*.rs",
    "ts": "*.ts",
    "tsx": "*.tsx",
}


def _require_non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _limit(value: object, default: int = _DEFAULT_LIMIT) -> int:
    limit = int(value or default)
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, _MAX_LIMIT)


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return [pattern]
    choices = match.group(1).split(",")
    expanded: list[str] = []
    for choice in choices:
        expanded.extend(
            _expand_braces(pattern[: match.start()] + choice + pattern[match.end() :])
        )
    return expanded


def _format_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


class GlobTool:
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="glob",
            description=(
                "Find files by shell-style glob pattern. Supports ** for recursive "
                "directory matching and simple brace groups such as *.{json,yaml}. "
                "Paths may be absolute or relative to the process working directory. "
                "Results are sorted by modification time, newest first, and capped "
                "at limit (default 100)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer", "default": _DEFAULT_LIMIT},
                },
                "required": ["pattern"],
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
        pattern = _require_non_empty_string(invocation.args.get("pattern"), "pattern")
        limit = _limit(invocation.args.get("limit"))

        paths: dict[Path, float] = {}
        for expanded in _expand_braces(pattern):
            for raw in globlib.iglob(
                expanded,
                recursive=True,
            ):
                path = Path(raw)
                if not path.is_file():
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                paths[path] = mtime

        ordered = sorted(paths.items(), key=lambda item: (-item[1], _format_path(item[0])))
        truncated = len(ordered) > limit
        selected = ordered[:limit]

        header = (
            f"Glob(pattern={pattern!r}, matches={len(ordered)}, limit={limit}, "
            f"truncated={str(truncated).lower()})"
        )
        if not selected:
            return ToolResult.success(content=header + "\nNo files matched.")

        lines = [header, *(_format_path(path) for path, _ in selected)]
        if truncated:
            lines.append("Results truncated; narrow the pattern or increase limit.")
        return ToolResult.success(content="\n".join(lines))


class GrepTool:
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="grep",
            description=(
                "Search file contents with ripgrep regex syntax. Defaults to "
                "files_with_matches. Set output_mode to content for matching lines "
                "with file and line number, or count for per-file match counts. "
                "Scope with path, glob, or type. Grep respects ignore files by "
                "default unless path points directly at a file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string"},
                    "type": {"type": "string"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                        "default": "files_with_matches",
                    },
                    "multiline": {"type": "boolean", "default": False},
                    "ignore_case": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": _DEFAULT_LIMIT},
                },
                "required": ["pattern"],
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
        pattern = _require_non_empty_string(invocation.args.get("pattern"), "pattern")
        path = str(invocation.args.get("path") or ".")
        output_mode = str(invocation.args.get("output_mode") or "files_with_matches")
        if output_mode not in {"files_with_matches", "content", "count"}:
            raise ValueError("output_mode must be files_with_matches, content, or count")
        limit = _limit(invocation.args.get("limit"))

        args = ["rg", "--color", "never", "--no-heading"]
        if output_mode == "files_with_matches":
            args.append("--files-with-matches")
        elif output_mode == "count":
            args.append("--count")
        else:
            args.append("--json")
        if invocation.args.get("multiline"):
            args.append("--multiline")
        if invocation.args.get("ignore_case"):
            args.append("--ignore-case")
        glob_pattern = invocation.args.get("glob")
        type_name = invocation.args.get("type")
        if isinstance(glob_pattern, str) and glob_pattern:
            args.extend(["--glob", glob_pattern])
        if isinstance(type_name, str) and type_name:
            args.extend(["--glob", _TYPE_GLOBS.get(type_name, f"*.{type_name}")])
        args.extend([pattern, path])

        try:
            completed = await anyio.run_process(args, check=False)
        except FileNotFoundError:
            raise ValueError("ripgrep executable 'rg' was not found on PATH") from None

        stdout = completed.stdout.decode(errors="replace")
        stderr = completed.stderr.decode(errors="replace").strip()
        if completed.returncode not in {0, 1}:
            return ToolResult(
                status=ToolResultStatus.ERROR,
                content="",
                error=ToolResult.from_error(
                    "ripgrep_failed",
                    stderr or f"rg failed with return code {completed.returncode}",
                    retryable=True,
                ).error,
            )

        rendered, total, truncated = self._render_output(stdout, output_mode, limit)
        header = (
            f"Grep(pattern={pattern!r}, path={path!r}, mode={output_mode}, "
            f"matches={total}, limit={limit}, truncated={str(truncated).lower()})"
        )
        if not rendered:
            return ToolResult.success(content=header + "\nNo matches found.")
        lines = [header, *rendered]
        if truncated:
            lines.append("Results truncated; narrow pattern/path/glob or increase limit.")
        return ToolResult.success(content="\n".join(lines))

    def _render_output(
        self, stdout: str, output_mode: str, limit: int
    ) -> tuple[list[str], int, bool]:
        if output_mode == "files_with_matches":
            paths = [
                _format_path(Path(line))
                for line in stdout.splitlines()
                if line.strip()
            ]
            return paths[:limit], len(paths), len(paths) > limit
        if output_mode == "count":
            counts = [
                line
                for line in (
                    self._render_count_line(raw_line)
                    for raw_line in stdout.splitlines()
                )
                if line is not None
            ]
            return counts[:limit], len(counts), len(counts) > limit
        return self._render_json_lines(stdout, limit)

    def _render_json_lines(
        self, stdout: str, limit: int
    ) -> tuple[list[str], int, bool]:
        lines: list[str] = []
        total = 0
        for raw_line in stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            data = event.get("data") or {}
            if event_type == "match":
                path = self._event_path(data)
                if path is None:
                    continue
                line_number = data.get("line_number")
                text = (data.get("lines") or {}).get("text", "").rstrip("\n\r")
                total += 1
                if len(lines) < limit:
                    lines.append(f"{path}:{line_number}: {text}")
        return lines, total, total > len(lines)

    def _render_count_line(self, raw_line: str) -> str | None:
        if not raw_line.strip():
            return None
        path_text, separator, count = raw_line.rpartition(":")
        if not separator:
            return raw_line
        return f"{_format_path(Path(path_text))}: {count}"

    def _event_path(self, data: dict) -> str | None:
        path = data.get("path", {}).get("text")
        if not isinstance(path, str):
            return None
        return _format_path(Path(path))
