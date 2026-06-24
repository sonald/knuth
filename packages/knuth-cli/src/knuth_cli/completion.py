"""Prompt completion for the interactive CLI REPL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import anyio
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from knuth.core.types import RunStatus
from knuth_cli.interactive_commands import builtin_command_catalog, load_command_catalog


@dataclass(frozen=True)
class RunCompletion:
    id: str
    status: str


@dataclass(frozen=True)
class ToolCompletion:
    name: str
    description: str = ""


@dataclass(frozen=True)
class CommandCompletion:
    name: str
    description: str = ""
    source: str = "builtin"


@dataclass(frozen=True)
class CompletionSnapshot:
    commands: tuple[CommandCompletion, ...] = ()
    runs: tuple[RunCompletion, ...] = ()
    tools: tuple[ToolCompletion, ...] = ()


class CompletionManager:
    """Owns best-effort runtime completion snapshots."""

    def __init__(self) -> None:
        self.snapshot = CompletionSnapshot(
            commands=_command_completions_from_catalog(builtin_command_catalog())
        )

    async def refresh(self, runtime, *, timeout: float = 1.0) -> None:
        with anyio.move_on_after(timeout):
            catalog = await load_command_catalog(runtime, best_effort=True)
            runs = await _load_runs(runtime)
            tools = await _load_tools(runtime)
            self.snapshot = CompletionSnapshot(
                commands=_command_completions_from_catalog(catalog),
                runs=tuple(runs),
                tools=tuple(tools),
            )


class KnuthCompleter(Completer):
    """Routes slash command completions without doing runtime work."""

    def __init__(self, manager: CompletionManager) -> None:
        self._manager = manager

    def get_completions(
        self, document: Document, complete_event
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        parts = text.split()
        trailing_space = text.endswith(" ")
        command = parts[0] if parts else text

        if not trailing_space and len(parts) <= 1:
            yield from _slash_command_completions(
                self._manager.snapshot.commands, command
            )
            return

        token = "" if trailing_space else parts[-1]
        if command in {"/resume", "/status"}:
            yield from _run_id_completions(self._manager.snapshot.runs, token)
        elif command == "/tools":
            yield from _tool_completions(self._manager.snapshot.tools, token)


async def _load_runs(runtime) -> list[RunCompletion]:
    runs_fn = getattr(runtime, "runs", None)
    if runs_fn is None:
        return []
    try:
        runs = await runs_fn(limit=20)
    except Exception:
        return []
    completions: list[RunCompletion] = []
    for run in runs:
        run_id = getattr(run, "id", None)
        if not isinstance(run_id, str) or not run_id:
            continue
        raw_status = getattr(run, "status", "")
        if isinstance(raw_status, RunStatus):
            status = raw_status.value
        else:
            status = str(raw_status)
        completions.append(RunCompletion(id=run_id, status=status))
    return completions


async def _load_tools(runtime) -> list[ToolCompletion]:
    tools_fn = getattr(runtime, "tools", None)
    if tools_fn is None:
        return []
    try:
        tools = await tools_fn()
    except Exception:
        return []
    completions: list[ToolCompletion] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function", {})
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = function.get("description")
        completions.append(
            ToolCompletion(
                name=name,
                description=description if isinstance(description, str) else "",
            )
        )
    return completions


def _command_completions_from_catalog(catalog) -> tuple[CommandCompletion, ...]:
    return tuple(
        CommandCompletion(
            name=f"/{command.name}",
            description=command.description,
            source=command.source,
        )
        for command in catalog.commands
    )


def _slash_command_completions(
    commands: Iterable[CommandCompletion], token: str
) -> Iterable[Completion]:
    for command in commands:
        if command.name.startswith(token):
            yield Completion(
                command.name,
                start_position=-len(token),
                display_meta=command.description,
            )


def _run_id_completions(
    runs: Iterable[RunCompletion], token: str
) -> Iterable[Completion]:
    for run in runs:
        if run.id.startswith(token):
            yield Completion(
                run.id,
                start_position=-len(token),
                display_meta=run.status,
            )


def _tool_completions(
    tools: Iterable[ToolCompletion], token: str
) -> Iterable[Completion]:
    for tool in tools:
        if tool.name.startswith(token):
            yield Completion(
                tool.name,
                start_position=-len(token),
                display_meta=tool.description,
            )
