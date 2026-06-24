from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import Field

from knuth.core.skills import SkillInfo
from knuth.core.types import KnuthModel


class CommandSpec(KnuthModel):
    name: str
    description: str
    source: str
    canonical: str | None = None
    skill_name: str | None = None


class CommandCatalog(KnuthModel):
    commands: list[CommandSpec] = Field(default_factory=list)

    def resolve(self, name: str) -> CommandSpec | None:
        for command in self.commands:
            if command.name == name:
                return command
        return None


class CommandInvocation(KnuthModel):
    name: str
    raw_args: str
    surface: str
    command: CommandSpec


DEFAULT_BUILTIN_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(name="help", description="Show help", source="builtin"),
    CommandSpec(name="tools", description="List available tools", source="builtin"),
    CommandSpec(name="new", description="Start a fresh conversation", source="builtin"),
    CommandSpec(name="clear", description="Start a fresh conversation", source="builtin"),
    CommandSpec(
        name="resume",
        description="Resume the current or specified waiting/paused run",
        source="builtin",
    ),
    CommandSpec(name="status", description="Show the current run status", source="builtin"),
    CommandSpec(name="skill", description="Request a skill by name", source="builtin"),
    CommandSpec(name="usage", description="Show token usage for a run", source="builtin"),
    CommandSpec(name="exit", description="Leave the session", source="builtin"),
    CommandSpec(name="quit", description="Leave the session", source="builtin"),
)


def build_command_catalog(
    builtin_specs: Sequence[CommandSpec],
    skill_infos: Iterable[SkillInfo],
) -> CommandCatalog:
    commands = list(builtin_specs)
    reserved = {command.name for command in commands}
    commands.extend(project_skill_commands(skill_infos, reserved))
    return CommandCatalog(commands=commands)


def project_skill_commands(
    skill_infos: Iterable[SkillInfo],
    reserved_names: set[str] | frozenset[str],
) -> list[CommandSpec]:
    commands: list[CommandSpec] = []
    for info in sorted(skill_infos, key=lambda item: item.metadata.name):
        name = info.metadata.name
        canonical = f"skill:{name}"
        commands.append(
            CommandSpec(
                name=canonical,
                description=info.metadata.description,
                source="skill",
                canonical=canonical,
                skill_name=name,
            )
        )
        if name not in reserved_names:
            commands.append(
                CommandSpec(
                    name=name,
                    description=info.metadata.description,
                    source="skill",
                    canonical=canonical,
                    skill_name=name,
                )
            )
    return commands


def parse_slash_invocation(
    text: str,
    catalog: CommandCatalog,
    *,
    surface: str = "slash",
) -> CommandInvocation | None:
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    token, raw_args = _split_command_body(body)
    if not token:
        return None
    command = catalog.resolve(token)
    if command is None:
        return None
    return CommandInvocation(
        name=token,
        raw_args=raw_args,
        surface=surface,
        command=command,
    )


def render_skill_command_prompt(skill_name: str, raw_args: str) -> str:
    header = f"Use the `{skill_name}` skill for this request before answering."
    if not raw_args:
        return header
    return "\n\n".join([header, "Skill command arguments:\n" + raw_args])


def _split_command_body(body: str) -> tuple[str, str]:
    for index, char in enumerate(body):
        if char.isspace():
            return body[:index], body[index + 1 :]
    return body, ""


__all__ = [
    "CommandCatalog",
    "CommandInvocation",
    "CommandSpec",
    "DEFAULT_BUILTIN_COMMAND_SPECS",
    "build_command_catalog",
    "parse_slash_invocation",
    "project_skill_commands",
    "render_skill_command_prompt",
]
