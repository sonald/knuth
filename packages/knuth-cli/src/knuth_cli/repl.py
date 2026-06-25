"""Interactive, Claude Code style REPL for the Knuth agent."""

from __future__ import annotations

import signal
import sys

import anyio
from knuth.core.commands import (
    CommandInvocation,
    parse_slash_invocation,
    render_skill_command_prompt,
)
from knuth_runtime import AgentRuntime, LedgerError, RunResult, RuntimeObservationError
from rich.console import Console
from rich.text import Text

from knuth.core.types import RunStatus
from knuth_cli.completion import CompletionManager, KnuthCompleter
from knuth_cli.input import PromptInput, PromptToolkitInput, StreamInput
from knuth_cli.input_history import PromptHistory
from knuth_cli.interactive_commands import builtin_command_catalog, load_command_catalog
from knuth_cli.render import EventRenderer

_BANNER = "Knuth agent ready. Type /help for commands, /exit to quit."
_PROMPT = "knuth ❯ "
_EXIT_INTERRUPTED = 130

# How long a graceful interrupt may run before the driver force-cancels the
# turn. Ctrl-C is a cooperative stop first; this deadline (or a second Ctrl-C)
# is the force-stop escape hatch so a tool that never reaches a safe point
# cannot pin the foreground forever.
_INTERRUPT_DEADLINE_S = 5.0


class _TurnForced(Exception):
    """The turn was force-stopped (second Ctrl-C or deadline) before it could
    record a clean durable outcome."""

    def __init__(self, run_id: str | None) -> None:
        super().__init__("turn force-stopped")
        self.run_id = run_id


_ACTIONABLE_STATUSES = frozenset(
    {
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_TOOL_RESULT,
        RunStatus.PAUSED,
        RunStatus.RUNNING,
    }
)


def _make_prompt_history() -> PromptHistory:
    return PromptHistory()


def _make_prompt_input(
    runtime: AgentRuntime,
    console: Console,
    history: PromptHistory | None = None,
    completion_manager: CompletionManager | None = None,
) -> PromptInput:
    if _stdio_is_interactive(console):
        if history is None:
            history = _make_prompt_history()
        completer = (
            KnuthCompleter(completion_manager)
            if completion_manager is not None
            else None
        )
        return PromptToolkitInput(history=history, completer=completer)
    return StreamInput(console)


def _stdio_is_interactive(console: Console) -> bool:
    stdin_is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    output = getattr(console, "file", sys.stdout)
    stdout_is_tty = bool(getattr(output, "isatty", lambda: False)())
    return stdin_is_tty and stdout_is_tty


async def _reenter_actionable(
    runtime: AgentRuntime,
    console: Console,
    allowed_tools: set[str],
    prompt_input: PromptInput,
) -> str | None:
    """On entering interactive mode, restore the latest actionable run.

    Rather than a blank prompt, re-show a pending approval, the external-result
    wait, or a resumable paused run so the user does not have to reconstruct
    state with external commands. A ``RUNNING`` run with no live session in this
    process is not auto-recovered (another process may still drive it).
    """
    runs_fn = getattr(runtime, "runs", None)
    if runs_fn is None:
        return None
    try:
        runs = await runs_fn(limit=20)
    except Exception:
        return None
    actionable = [run for run in runs if run.status in _ACTIONABLE_STATUSES]
    if not actionable:
        return None
    if len(actionable) > 1:
        console.print(
            Text("Multiple runs need attention; pick one to resume:", style="yellow")
        )
        for run in actionable:
            console.print(
                Text(f"  {run.id} · {run.status.value}", style="dim")
            )
        console.print(Text("Use `knuth resume <id>` to choose.", style="dim"))
        return None
    run = actionable[0]
    if run.status == RunStatus.WAITING_APPROVAL:
        console.print(
            Text(f"Resuming run {run.id}, waiting for approval:", style="yellow")
        )
        await _resolve_approvals(
            runtime,
            console,
            RunResult(answer="", run_id=run.id, status=RunStatus.WAITING_APPROVAL),
            run.id,
            allowed_tools,
            prompt_input,
        )
        return run.id
    if run.status == RunStatus.WAITING_TOOL_RESULT:
        console.print(
            Text(
                f"Run {run.id} is waiting for an external tool result.",
                style="yellow",
            )
        )
        return run.id
    if run.status == RunStatus.PAUSED:
        console.print(
            Text(
                f"Run {run.id} is paused; resume with `knuth resume {run.id}`.",
                style="yellow",
            )
        )
        return run.id
    # RUNNING with no live session in this process: do not auto-recover.
    console.print(
        Text(
            f"Run {run.id} is RUNNING but this process holds no live session;"
            " cannot attach. Use `knuth recover` if it is truly stranded.",
            style="yellow",
        )
    )
    return None


async def run_interactive(runtime: AgentRuntime, console: Console) -> int:
    console.print(Text(_BANNER, style="bold"))
    allowed_tools: set[str] = set()
    history = _make_prompt_history()
    completion_manager = CompletionManager()
    try:
        prompt_input = _make_prompt_input(runtime, console, history, completion_manager)
    except Exception as exc:
        console.print(
            Text(
                f"Could not initialize interactive input: "
                f"{exc.__class__.__name__}: {exc}",
                style="bold red",
            )
        )
        return 1
    async with anyio.create_task_group() as tg:
        _schedule_completion_refresh(tg, completion_manager, runtime)
        session_run_id: str | None = await _reenter_actionable(
            runtime, console, allowed_tools, prompt_input
        )
        while True:
            input_result = await prompt_input.read_prompt(_PROMPT)
            if input_result.kind == "cancelled":
                console.print()
                continue
            if input_result.kind == "eof":
                console.print()
                tg.cancel_scope.cancel()
                return 0
            prompt = input_result.text.strip()
            if not prompt:
                continue
            if prompt in {"/exit", "/quit"}:
                tg.cancel_scope.cancel()
                return 0
            if prompt.startswith("/"):
                try:
                    invocation = await _parse_cli_command(runtime, prompt)
                except Exception as exc:
                    console.print(
                        Text(
                            "Could not load commands: "
                            f"{exc.__class__.__name__}: {exc}",
                            style="bold red",
                        )
                    )
                    continue
                if invocation is not None:
                    if prompt in {"/new", "/clear"}:
                        history.start_new_session()
                    if getattr(
                        prompt_input, "records_history", False
                    ) and await _command_writes_prompt_history(runtime, invocation):
                        history.append_prompt(prompt)
                    session_run_id = await _handle_slash(
                        runtime,
                        console,
                        prompt,
                        session_run_id,
                        allowed_tools,
                        prompt_input,
                        invocation=invocation,
                    )
                    _schedule_completion_refresh(tg, completion_manager, runtime)
                    continue
            if getattr(prompt_input, "records_history", False):
                history.append_prompt(prompt)
            try:
                session_run_id, result = await _run_turn(
                    runtime, console, prompt, session_run_id, allowed_tools, prompt_input
                )
                _schedule_completion_refresh(tg, completion_manager, runtime)
                if result is not None and result.status == RunStatus.INTERRUPTED:
                    console.print(
                        Text(
                            f"run {session_run_id} · interrupted"
                            " (send a new message to continue)",
                            style="dim",
                        )
                    )
            except Exception as exc:
                console.print(
                    Text(
                        f"Run failed: {exc.__class__.__name__}: {exc}",
                        style="bold red",
                    )
                )


def _schedule_completion_refresh(
    task_group: anyio.abc.TaskGroup,
    manager: CompletionManager,
    runtime: AgentRuntime,
) -> None:
    task_group.start_soon(manager.refresh, runtime)


async def _parse_cli_command(runtime: AgentRuntime, prompt: str):
    builtin_invocation = parse_slash_invocation(
        prompt, builtin_command_catalog(), surface="cli.slash"
    )
    if builtin_invocation is not None:
        return builtin_invocation
    catalog = await load_command_catalog(runtime)
    return parse_slash_invocation(prompt, catalog, surface="cli.slash")


async def _command_writes_prompt_history(
    runtime: AgentRuntime,
    invocation: CommandInvocation,
) -> bool:
    if invocation.command.source == "skill":
        return True
    if invocation.name != "skill":
        return False
    parts = invocation.raw_args.split(maxsplit=1)
    return bool(parts) and await _has_skill(runtime, parts[0], best_effort=True)


async def run_single(runtime: AgentRuntime, console: Console, prompt: str) -> int:
    """Render a single streaming turn (used for ``knuth run <prompt>``)."""
    run_id: str | None = None
    prompt_input = _make_prompt_input(runtime, console)
    try:
        run_id, result = await _run_turn(
            runtime, console, prompt, None, set(), prompt_input
        )
    except Exception as exc:
        console.print(
            Text(f"Run failed: {exc.__class__.__name__}: {exc}", style="bold red")
        )
        return 1
    if result is None:
        # Force-stopped before a clean outcome.
        return _EXIT_INTERRUPTED
    status = result.status
    footer = f"run {run_id} · {status.value if status else 'unknown'}"
    console.print(Text(footer, style="dim"))
    if status == RunStatus.SUCCEEDED:
        return 0
    if status == RunStatus.INTERRUPTED:
        console.print(
            Text(
                f"Run was interrupted. Continue with `knuth run --once` or a new"
                f" message in `knuth run` on run {run_id}.",
                style="yellow",
            )
        )
        return _EXIT_INTERRUPTED
    if status == RunStatus.WAITING_APPROVAL:
        console.print(
            Text(
                f"Run is waiting for approval. Approve with `knuth approve <id>` "
                f"then `knuth resume {run_id}`.",
                style="yellow",
            )
        )
        return 2
    if status in {RunStatus.PAUSED}:
        console.print(
            Text(f"Run is paused. Resume with `knuth resume {run_id}`.", style="yellow")
        )
        return 2
    return 1


async def run_resume(runtime: AgentRuntime, console: Console, run_id: str) -> int:
    """Resume a paused or waiting run with live rendering and approvals."""
    prompt_input = _make_prompt_input(runtime, console)
    try:
        result = await _resume_existing_run(runtime, console, run_id, set(), prompt_input)
    except _TurnForced as forced:
        console.print(Text("⊘ interrupted (forced)", style="yellow"))
        if forced.run_id:
            console.print(Text(f"run {forced.run_id} · unknown", style="dim"))
        return _EXIT_INTERRUPTED
    except LedgerError as exc:
        console.print(Text(f"Cannot resume run {run_id}: {exc}", style="bold red"))
        return 2
    if result is None:
        return 2
    status = result.status
    console.print(
        Text(f"run {run_id} · {status.value if status else 'unknown'}", style="dim")
    )
    if status == RunStatus.SUCCEEDED:
        return 0
    if status in {
        RunStatus.INTERRUPTED,
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_TOOL_RESULT,
        RunStatus.PAUSED,
        RunStatus.RUNNING,
    }:
        return 2
    return 1


async def _resume_existing_run(
    runtime: AgentRuntime,
    console: Console,
    run_id: str,
    allowed_tools: set[str],
    prompt_input: PromptInput,
) -> RunResult | None:
    """Resume a durable control point using the foreground interrupt driver."""
    status_fn = getattr(runtime, "status", None)
    status: RunStatus | None = None
    if status_fn is not None:
        try:
            status = await status_fn(run_id)
        except KeyError:
            # Surface an unknown run as a ledger error so the caller's
            # existing handler renders it as a friendly message instead of
            # letting the later ``runtime.resume()`` crash the task group.
            raise LedgerError(f"unknown run: {run_id}") from None
        except Exception:
            status = None
    if status == RunStatus.INTERRUPTED:
        console.print(
            Text(
                f"Run {run_id} was interrupted; send a new message to continue.",
                style="yellow",
            )
        )
        return RunResult(answer="", run_id=run_id, status=status)
    if status == RunStatus.RUNNING:
        console.print(
            Text(
                f"Run {run_id} is RUNNING but this process holds no live session;"
                " cannot attach. Use `knuth recover` if it is truly stranded.",
                style="yellow",
            )
        )
        return RunResult(answer="", run_id=run_id, status=status)

    # A run waiting for approval cannot be resumed until the approval is
    # resolved; rather than crash on the ledger error, route into the approval UI.
    pending_fn = getattr(runtime, "pending_approvals", None)
    pending = await pending_fn(run_id) if pending_fn is not None else []
    if pending:
        return await _resolve_approvals(
            runtime,
            console,
            RunResult(answer="", run_id=run_id, status=RunStatus.WAITING_APPROVAL),
            run_id,
            allowed_tools,
            prompt_input,
        )

    renderer = EventRenderer(console)
    try:
        async with runtime.resume(run_id, listeners=[renderer]) as session:
            result = await _drive_session_to_result(
                console, session, allow_interrupt=True
            )
    finally:
        renderer.finish()
    return await _resolve_approvals(
        runtime, console, result, run_id, allowed_tools, prompt_input
    )


async def _drive_session_to_result(
    console: Console, session, *, allow_interrupt: bool
) -> RunResult:
    """Await a session result while making Ctrl-C a graceful interrupt.

    The first SIGINT triggers the live interrupt signal: the agent loop reaches
    its safe point and the run resolves to ``INTERRUPTED`` (not ``PAUSED``). A
    second SIGINT, or the deadline, force-cancels the turn — the escape hatch,
    which may leave durable state for recovery rather than a clean outcome.
    """
    result_holder: list[RunResult | None] = [None]
    forced = [False]

    try:
        async with anyio.create_task_group() as tg:

            async def _await_result() -> None:
                result_holder[0] = await session.result()
                tg.cancel_scope.cancel()

            async def _deadline() -> None:
                await anyio.sleep(_INTERRUPT_DEADLINE_S)
                forced[0] = True
                tg.cancel_scope.cancel()

            async def _watch_sigint() -> None:
                interrupt = getattr(session, "interrupt", None)
                with anyio.open_signal_receiver(signal.SIGINT) as signals:
                    count = 0
                    async for _ in signals:
                        count += 1
                        if count == 1 and interrupt is not None:
                            interrupt("user_stop")
                            console.print(
                                Text(
                                    "⊘ interrupting… (Ctrl-C again to force)",
                                    style="yellow",
                                )
                            )
                            tg.start_soon(_deadline)
                            continue
                        forced[0] = True
                        tg.cancel_scope.cancel()
                        return

            if allow_interrupt:
                tg.start_soon(_watch_sigint)
            tg.start_soon(_await_result)
    except BaseExceptionGroup as group:
        # The task group wraps a session/result error; surface the single
        # underlying exception so callers see the real cause.
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise

    if result_holder[0] is None:
        raise _TurnForced(getattr(session, "run_id", None))
    return result_holder[0]


async def _run_turn(
    runtime: AgentRuntime,
    console: Console,
    prompt: str,
    session_run_id: str | None,
    allowed_tools: set[str],
    prompt_input: PromptInput,
) -> tuple[str | None, RunResult | None]:
    renderer = EventRenderer(console)
    session_factory = (
        runtime.start(prompt, listeners=[renderer])
        if session_run_id is None
        else runtime.continue_run(session_run_id, prompt, listeners=[renderer])
    )
    run_id = session_run_id
    result: RunResult | None = None
    try:
        async with session_factory as session:
            run_id = getattr(session, "run_id", session_run_id)
            try:
                result = await _drive_session_to_result(
                    console, session, allow_interrupt=True
                )
            except _TurnForced as forced:
                console.print(Text("⊘ interrupted (forced)", style="yellow"))
                return forced.run_id or run_id, None
    except RuntimeObservationError as exc:
        result = exc.result if isinstance(exc.result, RunResult) else None
        console.print(
            Text(
                f"Display error while rendering run {exc.run_id}.",
                style="bold red",
            )
        )
        for failure in exc.failures:
            console.print(
                Text(
                    f"  {failure.listener_name}: {failure.error}",
                    style="red",
                )
            )
        if result is None:
            return session_run_id, None
    finally:
        renderer.finish()
    run_id = result.run_id
    try:
        result = await _resolve_approvals(
            runtime, console, result, run_id, allowed_tools, prompt_input
        )
    except _TurnForced as forced:
        console.print(Text("⊘ interrupted (forced)", style="yellow"))
        return forced.run_id or run_id, None
    return run_id, result


async def _resolve_approvals(
    runtime: AgentRuntime,
    console: Console,
    result: RunResult,
    run_id: str | None,
    allowed_tools: set[str],
    prompt_input: PromptInput,
) -> RunResult:
    while result.status == RunStatus.WAITING_APPROVAL and run_id is not None:
        pending = await runtime.pending_approvals(run_id)
        if not pending:
            break
        tool_names = {
            approval.tool_call_id: tool
            for approval in pending
            if (tool := str(approval.approval_preview.get("tool") or ""))
        }
        for approval in pending:
            tool = str(approval.approval_preview.get("tool") or "")
            if tool and tool in allowed_tools:
                await runtime.approve(approval.id)
                console.print(
                    Text(f"  ✔ auto-approved {tool} (session)", style="dim")
                )
                continue
            label = tool or approval.title
            answer = await prompt_input.read_approval(f"  approve {label}? [y/N/a] ")
            if answer.kind in {"cancelled", "eof"}:
                _print_approval_handoff(console, run_id, pending)
                return result
            choice = answer.text.strip().lower()
            if choice in {"a", "always"}:
                if tool:
                    allowed_tools.add(tool)
                await runtime.approve(approval.id)
            elif choice in {"y", "yes"}:
                await runtime.approve(approval.id)
            else:
                await runtime.deny(approval.id)
                console.print(Text(f"  ✘ denied {label}", style="dim"))
        renderer = EventRenderer(console)
        renderer.remember_tool_names(tool_names)
        try:
            async with runtime.resume(run_id, listeners=[renderer]) as session:
                result = await _drive_session_to_result(
                    console, session, allow_interrupt=True
                )
        finally:
            renderer.finish()
    return result


def _print_approval_handoff(console: Console, run_id: str, pending: list) -> None:
    """Input is gone (EOF/Ctrl-C); leave approvals pending instead of denying."""
    console.print(
        Text(
            "Input closed; leaving the run waiting for approval.",
            style="yellow",
        )
    )
    for approval in pending:
        console.print(Text(f"  knuth approve {approval.id}", style="dim"))
    console.print(Text(f"  knuth resume {run_id}", style="dim"))


async def _handle_slash(
    runtime: AgentRuntime,
    console: Console,
    command: str,
    session_run_id: str | None,
    allowed_tools: set[str],
    prompt_input: PromptInput,
    *,
    invocation: CommandInvocation | None = None,
) -> str | None:
    if invocation is not None and invocation.command.source == "skill":
        skill_name = invocation.command.skill_name
        if skill_name is None:
            console.print(Text(f"Skill not available: {invocation.name}", style="red"))
            return session_run_id
        return await _run_skill_turn(
            runtime,
            console,
            skill_name,
            invocation.raw_args,
            session_run_id,
            allowed_tools,
            prompt_input,
        )

    parts = command.split()
    name = parts[0]
    if name == "/help":
        await _print_command_help(runtime, console)
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
    elif name == "/resume":
        target_run_id = parts[1] if len(parts) > 1 else session_run_id
        if len(parts) > 2:
            console.print(Text("Usage: /resume [run_id]", style="red"))
            return session_run_id
        if target_run_id is None:
            target_run_id = await _find_single_actionable_run_id(runtime, console)
        if target_run_id is None:
            return session_run_id
        try:
            result = await _resume_existing_run(
                runtime, console, target_run_id, allowed_tools, prompt_input
            )
        except _TurnForced as forced:
            console.print(Text("⊘ interrupted (forced)", style="yellow"))
            return forced.run_id or target_run_id
        except LedgerError as exc:
            console.print(
                Text(f"Cannot resume run {target_run_id}: {exc}", style="bold red")
            )
            # Resume failed — keep the previously-attached run as the
            # session run, so a stray ``/resume <typo>`` cannot rebind
            # subsequent ``/resume`` (no args) to a nonexistent id.
            return session_run_id
        if result is not None:
            status = result.status
            console.print(
                Text(
                    f"run {result.run_id} · {status.value if status else 'unknown'}",
                    style="dim",
                )
            )
            if status == RunStatus.INTERRUPTED:
                console.print(
                    Text("Send a new message to continue this run.", style="dim")
                )
            return result.run_id
        return target_run_id
    elif name == "/skill":
        raw_args = invocation.raw_args if invocation is not None else command[6:].lstrip()
        skill_parts = raw_args.split(maxsplit=1)
        if not skill_parts:
            console.print(Text("Usage: /skill <skill_name> [args]", style="red"))
            return session_run_id
        skill_name = skill_parts[0]
        skill_args = skill_parts[1] if len(skill_parts) > 1 else ""
        try:
            has_skill = await _has_skill(runtime, skill_name)
        except Exception as exc:
            console.print(
                Text(
                    f"Could not load skills: {exc.__class__.__name__}: {exc}",
                    style="bold red",
                )
            )
            return session_run_id
        if not has_skill:
            console.print(Text(f"Skill not found: {skill_name}", style="red"))
            return session_run_id
        return await _run_skill_turn(
            runtime,
            console,
            skill_name,
            skill_args,
            session_run_id,
            allowed_tools,
            prompt_input,
        )
    elif name == "/usage":
        raw_args = invocation.raw_args if invocation is not None else " ".join(parts[1:])
        usage_parts = raw_args.split()
        if len(usage_parts) > 1:
            console.print(Text("Usage: /usage [run_id]", style="red"))
            return session_run_id
        target_run_id = usage_parts[0] if usage_parts else session_run_id
        if target_run_id is None:
            console.print(Text("No active run. Use /usage <run_id>.", style="dim"))
            return session_run_id
        await _print_run_usage(runtime, console, target_run_id)
    else:
        console.print(Text(f"Unknown command: {name}", style="red"))
    return session_run_id


async def _print_command_help(runtime: AgentRuntime, console: Console) -> None:
    catalog = await load_command_catalog(runtime, best_effort=True)
    console.print("Commands:")
    for command in catalog.commands:
        if command.source == "skill" and command.name != command.canonical:
            continue
        console.print(
            Text(f"  /{command.name:<20}", style="bold")
            + Text(command.description, style="dim")
        )


async def _print_run_usage(
    runtime: AgentRuntime,
    console: Console,
    run_id: str,
) -> None:
    events_fn = getattr(runtime, "events", None)
    if events_fn is None:
        console.print(Text(f"Usage unavailable for run {run_id}.", style="dim"))
        return
    try:
        events = await events_fn(run_id)
    except KeyError:
        console.print(Text(f"Run not found: {run_id}", style="red"))
        return
    except Exception as exc:
        console.print(
            Text(
                f"Could not read usage for {run_id}: {exc.__class__.__name__}: {exc}",
                style="bold red",
            )
        )
        return
    if not events:
        console.print(Text(f"Run not found: {run_id}", style="red"))
        return

    calls = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    cost_usd = 0.0
    has_input = False
    has_output = False
    has_total = False
    has_cost = False

    for event in events:
        if getattr(event, "type", None) != "model.completed":
            continue
        usage = getattr(event, "usage", None)
        if usage is None:
            continue
        calls += 1
        value = getattr(usage, "input_tokens", None)
        if value is not None:
            has_input = True
            input_tokens += int(value)
        value = getattr(usage, "output_tokens", None)
        if value is not None:
            has_output = True
            output_tokens += int(value)
        value = getattr(usage, "total_tokens", None)
        if value is not None:
            has_total = True
            total_tokens += int(value)
        value = getattr(usage, "cost_usd", None)
        if value is not None:
            has_cost = True
            cost_usd += float(value)

    if calls == 0:
        console.print(Text(f"Usage unavailable for run {run_id}.", style="dim"))
        return

    console.print(Text(f"{run_id} usage", style="bold"))
    console.print(Text(f"  model calls: {calls}", style="dim"))
    console.print(
        Text(
            "  input tokens: "
            + (_format_int(input_tokens) if has_input else "unavailable"),
            style="dim",
        )
    )
    console.print(
        Text(
            "  output tokens: "
            + (_format_int(output_tokens) if has_output else "unavailable"),
            style="dim",
        )
    )
    console.print(
        Text(
            "  total tokens: "
            + (_format_int(total_tokens) if has_total else "unavailable"),
            style="dim",
        )
    )
    console.print(
        Text(
            "  cost: " + (f"${cost_usd:.6f}" if has_cost else "unavailable"),
            style="dim",
        )
    )


def _format_int(value: int) -> str:
    return f"{value:,}"


async def _run_skill_turn(
    runtime: AgentRuntime,
    console: Console,
    skill_name: str,
    raw_args: str,
    session_run_id: str | None,
    allowed_tools: set[str],
    prompt_input: PromptInput,
) -> str | None:
    prompt = render_skill_command_prompt(skill_name, raw_args)
    try:
        run_id, result = await _run_turn(
            runtime, console, prompt, session_run_id, allowed_tools, prompt_input
        )
    except Exception as exc:
        console.print(
            Text(f"Run failed: {exc.__class__.__name__}: {exc}", style="bold red")
        )
        return session_run_id
    if result is not None and result.status == RunStatus.INTERRUPTED:
        console.print(
            Text(
                f"run {run_id} · interrupted (send a new message to continue)",
                style="dim",
            )
        )
    return run_id


async def _has_skill(
    runtime: AgentRuntime,
    skill_name: str,
    *,
    best_effort: bool = False,
) -> bool:
    skills_fn = getattr(runtime, "skills", None)
    if skills_fn is None:
        return False
    try:
        skills = await skills_fn()
    except Exception:
        if not best_effort:
            raise
        return False
    return any(
        getattr(getattr(skill, "metadata", skill), "name", None) == skill_name
        for skill in skills
    )


async def _find_single_actionable_run_id(
    runtime: AgentRuntime, console: Console
) -> str | None:
    runs_fn = getattr(runtime, "runs", None)
    if runs_fn is None:
        console.print(Text("No active run to resume.", style="dim"))
        return None
    try:
        runs = await runs_fn(limit=20)
    except Exception as exc:
        console.print(
            Text(
                f"Could not list runs: {exc.__class__.__name__}: {exc}",
                style="bold red",
            )
        )
        return None
    actionable = [run for run in runs if run.status in _ACTIONABLE_STATUSES]
    if not actionable:
        console.print(Text("No active run to resume.", style="dim"))
        return None
    if len(actionable) > 1:
        console.print(
            Text("Multiple runs need attention; use /resume <id>:", style="yellow")
        )
        for run in actionable:
            console.print(Text(f"  {run.id} · {run.status.value}", style="dim"))
        return None
    return actionable[0].id
