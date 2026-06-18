from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import anyio
import yaml
from pydantic import Field, ValidationError

from knuth.core.invocations import ToolEffect, ToolInvocation, ToolRisk
from knuth.core.skills import SkillInfo, SkillMetadata, SkillSource
from knuth.core.tools import ToolResult
from knuth.core.types import KnuthModel

from knuth_toold.base import ToolManifest, ToolRuntimeContext

_KNOWN_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}

_SKILL_TOOL_DESCRIPTION = """Execute a Knuth skill within the main conversation.

Skills provide specialized instructions and domain knowledge. When the user's
task matches an available skill, invoke this tool before answering the task.

Important rules:
- Only invoke skills listed in the current <system-reminder> message for this
  model request.
- Do not guess skill names from memory or from earlier turns.
- If the current reminder says no skills are available, continue without using
  this tool.
- Do not invoke a skill that is already active in the current tool batch.
- If a skill is missing or no longer available, continue the task without it.
- Skill directories are read-only capability sources. Do not create or modify
  files inside skill roots.
- If a skill needs to create output files, write them under a dedicated
  subdirectory of the current working directory such as ./tmp or ./artifacts.
"""

SKILL_OBSERVATION_CHAR_LIMIT = 4096


class SkillValidationError(ValueError):
    pass


class SkillRoot(KnuthModel):
    source: SkillSource
    path: str


class Skill(KnuthModel):
    info: SkillInfo
    base_dir: str
    content: str
    validation_warnings: list[str] = Field(default_factory=list)


class SkillSnapshot(KnuthModel):
    version: int
    catalog_digest: str
    skills: list[Skill] = Field(default_factory=list)


class SkillManager:
    def __init__(self, roots: list[SkillRoot]) -> None:
        self._roots = list(roots)
        self._snapshot = SkillSnapshot(
            version=0,
            catalog_digest=_catalog_digest([]),
            skills=[],
        )
        self._dirty = True
        self._last_invalidation_reason: str | None = "initial_load"
        self.last_refresh_was_dirty = False
        self.last_refresh_catalog_changed = False

    def invalidate(self, reason: str | os.PathLike[str] | None = None) -> None:
        self._dirty = True
        self._last_invalidation_reason = str(reason) if reason is not None else None

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def refresh_if_dirty(self) -> SkillSnapshot:
        if not self._dirty:
            self.last_refresh_was_dirty = False
            self.last_refresh_catalog_changed = False
            return self._snapshot
        skills = _load_skills(self._roots)
        catalog_digest = _catalog_digest(skills)
        self.last_refresh_was_dirty = True
        self.last_refresh_catalog_changed = (
            catalog_digest != self._snapshot.catalog_digest
        )
        self._snapshot = SkillSnapshot(
            version=self._snapshot.version + 1,
            catalog_digest=catalog_digest,
            skills=skills,
        )
        self._dirty = False
        self._last_invalidation_reason = None
        return self._snapshot

    def current_snapshot(self) -> SkillSnapshot:
        return self._snapshot

    def skill_root_candidates(self) -> list[Path]:
        return [Path(root.path) for root in self._roots]

    def list_skills(self) -> list[Skill]:
        return list(self.refresh_if_dirty().skills)

    def get_skill(self, name: str) -> Skill | None:
        for skill in self.refresh_if_dirty().skills:
            if skill.info.metadata.name == name:
                return skill
        return None

    def to_skill_infos(self) -> list[SkillInfo]:
        return [skill.info for skill in self.refresh_if_dirty().skills]


class SkillHotReloadService:
    def __init__(
        self,
        manager: SkillManager,
        *,
        poll_interval_s: float = 0.25,
        debounce_ms: int = 1000,
    ) -> None:
        self._manager = manager
        self._poll_interval_s = poll_interval_s
        self._debounce_s = max(0, debounce_ms) / 1000

    async def run(self) -> None:
        last = _roots_fingerprint(self._manager.skill_root_candidates())
        while True:
            await anyio.sleep(self._poll_interval_s)
            current = _roots_fingerprint(self._manager.skill_root_candidates())
            if current == last:
                continue
            if self._debounce_s:
                await anyio.sleep(self._debounce_s)
                current = _roots_fingerprint(self._manager.skill_root_candidates())
            self._manager.invalidate("skill files changed")
            last = current


class SkillToolProvider:
    name = "skills"

    def __init__(
        self,
        manager: SkillManager,
        *,
        max_observation_chars: int = SKILL_OBSERVATION_CHAR_LIMIT,
    ) -> None:
        self._manager = manager
        self._max_observation_chars = max_observation_chars

    async def list_tools(self) -> list[ToolManifest]:
        self._manager.refresh_if_dirty()
        return [
            ToolManifest(
                name="skill",
                description=_SKILL_TOOL_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill to load.",
                        },
                        "args": {
                            "type": "string",
                            "description": "Plain text arguments for the skill.",
                            "default": "",
                        },
                    },
                    "required": ["skill_name"],
                    "additionalProperties": False,
                },
                parallelable=False,
                cacheable=False,
                risk=ToolRisk.LOW,
                effect=ToolEffect.READ,
            )
        ]

    async def call_tool(
        self,
        invocation: ToolInvocation,
        ctx: ToolRuntimeContext,
    ) -> ToolResult:
        _ = ctx
        if invocation.tool_name != "skill":
            return ToolResult.from_error(
                "unknown_skill_tool",
                f"SkillToolProvider cannot execute tool: {invocation.tool_name}",
            )
        skill_name = invocation.args.get("skill_name")
        if not isinstance(skill_name, str) or not skill_name:
            return ToolResult.from_error(
                "invalid_skill_name",
                "skill_name must be a non-empty string",
            )
        raw_args = invocation.args.get("args", "")
        if not isinstance(raw_args, str):
            return ToolResult.from_error("invalid_skill_args", "args must be a string")

        skill = self._manager.get_skill(skill_name)
        if skill is None:
            return ToolResult.from_error(
                "skill_not_found",
                f"Skill '{skill_name}' is not available in the current skill list.",
            )
        observation = render_skill_tool_observation(skill, raw_args)
        if len(observation) > self._max_observation_chars:
            return ToolResult.from_error(
                "skill_content_too_large",
                "Skill "
                f"'{skill_name}' renders to {len(observation)} characters, exceeding "
                f"the v1 tool-result limit of {self._max_observation_chars} characters.",
            )

        return ToolResult.success(content=observation)


def render_skill_system_section_text() -> str:
    return (
        "## Skill\n"
        "- **Skill** tool is used to invoke user-invocable skills to accomplish "
        "user's request. IMPORTANT: Only use Skill for skills listed in the "
        "current `<system-reminder>...</system-reminder>` user message for the "
        "current turn - do not guess or use built-in CLI commands. Skills can "
        "be hot-reloaded (added/removed/modified) during a session, and the "
        "current reminder is the single source of truth for the *current* turn; "
        "always re-check that the skill exists there right before invoking it, "
        "and do not rely on memory from earlier turns. If the user asks about "
        "the current available skills, answer from the current reminder and do "
        "not rely on memory from earlier turns. CAVEAT: user scope skills are "
        "stored under the app's configured skill directories. Do NOT create or "
        "modify files inside the skill or config directories. If the skill "
        "needs to generate, create, or write any files/directories, it must "
        "write only to a dedicated subdirectory under the current working "
        "directory (recommended examples: `./tmp`, `./artifacts`); do not write "
        "directly into the cwd root. Create the subdirectory if missing. If a "
        "tool or script accepts an output path (e.g. --path/--output/--dir), "
        "you must explicitly set it to a dedicated cwd subdirectory and never "
        "rely on defaults. If you cannot set a safe output path, ask the user "
        "before continuing."
    )


def render_skills_reminder_text(snapshot: SkillSnapshot) -> str:
    lines = [
        "<system-reminder>",
        "The following skills are available for use with the Skill tool.",
        "This reminder is the source of truth for the current model request. "
        "Re-check it before invoking any skill, because skills may change "
        "between turns.",
        f"Current skills count: {len(snapshot.skills)}",
        "",
    ]
    lines.extend(_skills_list_lines(snapshot))
    lines.append("</system-reminder>")
    return "\n".join(lines)


def render_skill_change_notice_text(snapshot: SkillSnapshot) -> str:
    lines = [
        f'<knuth-skill-notice catalog-digest="{snapshot.catalog_digest}">',
        "Available skills have changed.",
        f"Current skills count: {len(snapshot.skills)}",
        "",
    ]
    lines.extend(_skills_list_lines(snapshot))
    lines.append("</knuth-skill-notice>")
    return "\n".join(lines)


def render_skill_tool_observation(skill: Skill, args: str) -> str:
    name = skill.info.metadata.name
    return "\n\n".join(
        [
            f"Skill '{name}' loaded successfully.",
            f"Base directory for this skill: {skill.base_dir}",
            skill.content,
            f"Skill arguments: {args}",
        ]
    )


def _skills_list_lines(snapshot: SkillSnapshot) -> list[str]:
    if not snapshot.skills:
        return ["- none: No skills available"]
    return [
        f"- {skill.info.metadata.name}: {skill.info.metadata.description}"
        for skill in snapshot.skills
    ]


def _load_skills(roots: list[SkillRoot]) -> list[Skill]:
    selected: dict[str, Skill] = {}
    for root in roots:
        root_path = Path(root.path).expanduser()
        if not root_path.exists() or not root_path.is_dir():
            continue
        for skill_file in _iter_skill_files(root_path):
            try:
                skill = _load_skill_file(skill_file, root.source)
            except SkillValidationError:
                continue
            name = skill.info.metadata.name
            if name not in selected:
                selected[name] = skill
    return sorted(selected.values(), key=lambda skill: skill.info.metadata.name)


def _iter_skill_files(root: Path):
    stack = [root]
    seen: set[tuple[int, int]] = set()
    while stack:
        directory = stack.pop()
        try:
            stat = directory.stat()
        except OSError:
            continue
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)

        skill_file = _find_skill_file(directory)
        if skill_file is not None:
            yield skill_file
            continue

        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in reversed(children):
            if child.name == ".git":
                continue
            try:
                if child.is_dir():
                    stack.append(child)
            except OSError:
                continue


def _find_skill_file(directory: Path) -> Path | None:
    try:
        candidates = [
            child
            for child in directory.iterdir()
            if child.is_file() and child.name.lower() == "skill.md"
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda child: (child.name != "SKILL.md", child.name.lower()),
    )[0]


def _load_skill_file(path: Path, source: SkillSource) -> Skill:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text, path)
    try:
        raw = yaml.safe_load(frontmatter) if frontmatter.strip() else {}
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SkillValidationError(f"{path}: frontmatter must be a mapping")
    warnings = [
        f"unknown frontmatter key: {key}"
        for key in sorted(set(raw) - _KNOWN_FRONTMATTER_KEYS)
    ]
    try:
        metadata = _metadata_from_frontmatter(raw, path)
    except (SkillValidationError, ValidationError) as exc:
        raise SkillValidationError(str(exc)) from exc
    if metadata.name != path.parent.name:
        raise SkillValidationError(
            f"{path}: skill name must match parent directory name"
        )
    return Skill(
        info=SkillInfo(
            metadata=metadata,
            source=source,
            file_path=str(path),
        ),
        base_dir=str(path.parent),
        content=body.strip(),
        validation_warnings=warnings,
    )


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError(f"{path}: SKILL.md must start with frontmatter")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillValidationError(f"{path}: missing closing frontmatter delimiter")


def _metadata_from_frontmatter(raw: dict[str, Any], path: Path) -> SkillMetadata:
    name = raw.get("name")
    description = raw.get("description")
    if not isinstance(name, str):
        raise SkillValidationError(f"{path}: name must be a string")
    if not isinstance(description, str):
        raise SkillValidationError(f"{path}: description must be a string")
    license_value = _optional_string(raw.get("license"), path, "license")
    compatibility = _optional_string(raw.get("compatibility"), path, "compatibility")
    metadata = raw.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise SkillValidationError(f"{path}: metadata must be a mapping")
    allowed_tools = _allowed_tools(raw.get("allowed-tools"), path)
    return SkillMetadata(
        name=name,
        description=description,
        license=license_value,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=allowed_tools,
    )


def _optional_string(value: Any, path: Path, field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SkillValidationError(f"{path}: {field_name} must be a string")
    return value


def _allowed_tools(value: Any, path: Path) -> list[str] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SkillValidationError(f"{path}: allowed-tools must be a string")
    return value.split()


def _catalog_digest(skills: list[Skill]) -> str:
    payload = [
        {
            "name": skill.info.metadata.name,
            "description": skill.info.metadata.description,
            "source": skill.info.source.value,
            "file_path": skill.info.file_path,
            "compatibility": skill.info.metadata.compatibility,
            "allowed_tools": skill.info.metadata.allowed_tools,
        }
        for skill in sorted(skills, key=lambda item: item.info.metadata.name)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _roots_fingerprint(roots: list[Path]) -> str:
    rows: list[tuple] = []
    for root in roots:
        root = root.expanduser()
        if root.exists():
            rows.extend(_existing_tree_fingerprint(root))
            continue
        parent = _nearest_existing_parent(root)
        rows.append(("pending", str(root), str(parent), *_stat_row(parent)))
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _existing_tree_fingerprint(root: Path) -> list[tuple]:
    rows: list[tuple] = []
    stack = [root]
    seen: set[tuple[int, int]] = set()
    while stack:
        path = stack.pop()
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            relative = str(path)
        rows.append((relative, "dir" if path.is_dir() else "file", stat.st_mtime_ns, stat.st_size))
        if not path.is_dir():
            continue
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in reversed(children):
            if child.name == ".git":
                continue
            stack.append(child)
    return rows


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _stat_row(path: Path) -> tuple[int | None, int | None]:
    try:
        stat = path.stat()
    except OSError:
        return (None, None)
    return (stat.st_mtime_ns, stat.st_size)


__all__ = [
    "Skill",
    "SkillHotReloadService",
    "SkillManager",
    "SkillRoot",
    "SkillSnapshot",
    "SkillToolProvider",
    "SkillValidationError",
    "SKILL_OBSERVATION_CHAR_LIMIT",
    "render_skill_change_notice_text",
    "render_skill_system_section_text",
    "render_skill_tool_observation",
    "render_skills_reminder_text",
]
