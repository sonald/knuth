"""Interactive, Claude Code style REPL for the Knuth agent."""

from __future__ import annotations

import anyio
from knuth_runtime import AgentRuntime, RunResult
from rich.console import Console
from rich.text import Text

from knuth.core.types import RunStatus
from knuth_cli.render import EventRenderer

_BANNER = "Knuth agent ready. Type /help for commands, /exit to quit."
_PROMPT = "knuth ❯ "
_HELP = """Commands:
  /help            Show this help
  /tools           List available tools
  /new, /clear     Start a fresh conversation
  /status          Show the current run status
  /exit, /quit     Leave the session"""


async def run_interactive(runtime: AgentRuntime, console: Console) -> int:
    console.print(Text(_BANNER, style="bold"))
    session_run_id: str | None = None
    while True:
        line = await _read_line(console, _PROMPT)
        if line is None:  # EOF (Ctrl-D)
            console.print()
            return 0
        prompt = line.strip()
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            return 0
        if prompt.startswith("/"):
            session_run_id = await _handle_slash(
                runtime, console, prompt, session_run_id
            )
            continue
        session_run_id = await _run_turn(runtime, console, prompt, session_run_id)


async def run_single(runtime: AgentRuntime, console: Console, prompt: str) -> int:
    """Render a single streaming turn (used for ``knuth run <prompt>``)."""
    await _run_turn(runtime, console, prompt, None)
    return 0


async def _run_turn(
    runtime: AgentRuntime, console: Console, prompt: str, session_run_id: str | None
) -> str | None:
    renderer = EventRenderer(console)
    result = await runtime.run_streaming(
        prompt, renderer.handle, run_id=session_run_id
    )
    renderer.finish()
    run_id = result.run_id
    result = await _resolve_approvals(runtime, console, result, run_id)
    if result.status == RunStatus.WAITING_USER:
        console.print(Text("  (answer the question above to continue)", style="dim"))
    return run_id


async def _resolve_approvals(
    runtime: AgentRuntime, console: Console, result: RunResult, run_id: str | None
) -> RunResult:
    while result.status == RunStatus.WAITING_APPROVAL and run_id is not None:
        pending = await runtime.pending_approvals(run_id)
        if not pending:
            break
        for approval in pending:
            console.print(
                Text(f"  ⚠ {approval.title}", style="yellow bold")
            )
            if approval.reason:
                console.print(Text(f"    {approval.reason}", style="dim"))
            answer = await _read_line(console, "  approve? [y/N] ")
            if answer is not None and answer.strip().lower() in {"y", "yes"}:
                await runtime.approve(approval.id)
            else:
                await runtime.deny(approval.id)
        renderer = EventRenderer(console)
        result = await runtime.run_streaming(None, renderer.handle, run_id=run_id)
        renderer.finish()
    return result


async def _handle_slash(
    runtime: AgentRuntime, console: Console, command: str, session_run_id: str | None
) -> str | None:
    name = command.split()[0]
    if name == "/help":
        console.print(_HELP)
    elif name == "/tools":
        for item in await runtime.tools():
            function = item.get("function", {})
            console.print(
                Text(f"  {function.get('name')}", style="bold")
                + Text(f"  {function.get('description', '')}", style="dim")
            )
    elif name in {"/new", "/clear"}:
        console.print(Text("Started a new conversation.", style="dim"))
        return None
    elif name == "/status":
        if session_run_id is None:
            console.print(Text("No active run.", style="dim"))
        else:
            status = await runtime.status(session_run_id)
            console.print(Text(f"{session_run_id}: {status.value}", style="dim"))
    else:
        console.print(Text(f"Unknown command: {name}", style="red"))
    return session_run_id


async def _read_line(console: Console, prompt: str) -> str | None:
    def _input() -> str | None:
        try:
            return console.input(prompt)
        except EOFError:
            return None

    return await anyio.to_thread.run_sync(_input)
