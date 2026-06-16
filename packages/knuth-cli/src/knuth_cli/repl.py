"""Interactive, Claude Code style REPL for the Knuth agent."""

from __future__ import annotations

import queue
import signal
import sys
import threading
from dataclasses import dataclass, field

import anyio
from knuth_runtime import AgentRuntime, LedgerError, RunResult, RuntimeObservationError
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


async def _reenter_actionable(
    runtime: AgentRuntime, console: Console, allowed_tools: set[str]
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
    session_run_id: str | None = await _reenter_actionable(
        runtime, console, allowed_tools
    )
    while True:
        try:
            line = await _read_line(console, _PROMPT)
        except KeyboardInterrupt:
            console.print()
            return 0
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
        try:
            session_run_id, result = await _run_turn(
                runtime, console, prompt, session_run_id, allowed_tools
            )
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


async def run_single(runtime: AgentRuntime, console: Console, prompt: str) -> int:
    """Render a single streaming turn (used for ``knuth run <prompt>``)."""
    run_id: str | None = None
    try:
        run_id, result = await _run_turn(runtime, console, prompt, None, set())
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
    # A run waiting for approval cannot be resumed until the approval is
    # resolved; rather than crash on the ledger error, route into the approval
    # UI. An INTERRUPTED run is not resumable at all — say so plainly.
    pending_fn = getattr(runtime, "pending_approvals", None)
    pending = await pending_fn(run_id) if pending_fn is not None else []
    if pending:
        result = await _resolve_approvals(
            runtime,
            console,
            RunResult(answer="", run_id=run_id, status=RunStatus.WAITING_APPROVAL),
            run_id,
            set(),
        )
        status = result.status
        console.print(
            Text(f"run {run_id} · {status.value if status else 'unknown'}", style="dim")
        )
        return 0 if status == RunStatus.SUCCEEDED else 2
    renderer = EventRenderer(console)
    try:
        async with runtime.resume(run_id, listeners=[renderer]) as session:
            result = await session.result()
    except LedgerError as exc:
        console.print(Text(f"Cannot resume run {run_id}: {exc}", style="bold red"))
        return 2
    finally:
        renderer.finish()
    result = await _resolve_approvals(runtime, console, result, run_id, set())
    status = result.status
    console.print(
        Text(f"run {run_id} · {status.value if status else 'unknown'}", style="dim")
    )
    if status == RunStatus.SUCCEEDED:
        return 0
    if status in {RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
        return 2
    return 1


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
    result = await _resolve_approvals(runtime, console, result, run_id, allowed_tools)
    return run_id, result


async def _resolve_approvals(
    runtime: AgentRuntime,
    console: Console,
    result: RunResult,
    run_id: str | None,
    allowed_tools: set[str],
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
            try:
                answer = await _read_line(console, f"  approve {label}? [y/N/a] ")
            except KeyboardInterrupt:
                answer = None
            if answer is None:
                _print_approval_handoff(console, run_id, pending)
                return result
            choice = answer.strip().lower()
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
        async with runtime.resume(run_id, listeners=[renderer]) as session:
            result = await session.result()
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


_DECODE_ERROR = object()

# Darwin defines IUTF8 (0x00004000) but Python's termios module does not
# always expose it; Linux exposes termios.IUTF8 directly.
_DARWIN_IUTF8 = 0x00004000


def _enable_utf8_erase() -> None:
    """Set IUTF8 on the stdin tty so canonical-mode backspace erases whole
    UTF-8 characters.

    Reads here happen in a worker thread without readline, so the kernel
    does the line editing. Without IUTF8 a backspace removes one byte, and
    editing CJK input leaves torn multibyte sequences in the line buffer.
    """
    try:
        import termios

        if not sys.stdin.isatty():
            return
        iutf8 = getattr(
            termios,
            "IUTF8",
            _DARWIN_IUTF8 if sys.platform == "darwin" else None,
        )
        if iutf8 is None:
            return
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        if not attrs[0] & iutf8:
            attrs[0] |= iutf8
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        # Best effort: the decode-error retry below remains the fallback.
        pass


@dataclass
class _ReadRequest:
    """One pending line read; ``abandoned`` drops the line instead of
    delivering it to a caller that already gave up (Ctrl-C, cancellation).

    ``wake`` is an optional thread-safe callback the async caller registers so
    the reader thread can notify it directly (set an ``anyio.Event`` via the
    loop), instead of the caller blocking an AnyIO worker on ``done.wait()``.
    A worker blocked there would outlive a Ctrl-C and stall interpreter exit.
    """

    done: threading.Event = field(default_factory=threading.Event)
    line: object = None
    abandoned: bool = False
    wake: object = None

    def resolve(self, line: object) -> None:
        self.line = line
        self.done.set()
        wake = self.wake
        if wake is not None:
            wake()


class _StdinReader:
    """Owns the only thread that ever reads stdin.

    ``input()`` in ad-hoc worker threads is unsafe here: a read abandoned by
    Ctrl-C leaves its thread blocked inside ``input()``, and the next prompt
    spawns a second reader racing it on the same buffer. Interleaved reads
    tear multibyte UTF-8 sequences apart (UnicodeDecodeError on CJK input).
    Serializing every read through one thread makes that impossible.
    """

    def __init__(self) -> None:
        self._requests: queue.Queue[_ReadRequest] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._tty_prepared = False

    def submit(self) -> _ReadRequest:
        if not self._tty_prepared:
            self._tty_prepared = True
            _enable_utf8_erase()
        request = _ReadRequest()
        self._requests.put(request)
        self._ensure_thread()
        return request

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="knuth-stdin-reader", daemon=True
                )
                self._thread.start()

    def _loop(self) -> None:
        while True:
            request = self._requests.get()
            raw = self._read_one_line()
            if request.abandoned:
                # The caller is gone; swallowing the line keeps it from
                # leaking into (or tearing) the next read.
                continue
            request.resolve(raw)

    def _read_one_line(self) -> object:
        """Read one line, decoding at the byte layer when possible.

        Decoding bytes ourselves keeps a torn UTF-8 sequence from poisoning
        the text wrapper's incremental decoder state: the bad line is fully
        consumed and the next read starts clean.
        """
        stdin = sys.stdin
        buffer = getattr(stdin, "buffer", None)
        try:
            if buffer is not None:
                raw_bytes = buffer.readline()
                if raw_bytes == b"":
                    return None
                encoding = getattr(stdin, "encoding", None) or "utf-8"
                return raw_bytes.decode(encoding).rstrip("\n")
            raw = stdin.readline()
        except UnicodeDecodeError:
            return _DECODE_ERROR
        except Exception:
            return None
        return None if raw == "" else raw.rstrip("\n")


_stdin_reader = _StdinReader()


async def _read_line(console: Console, prompt: str) -> str | None:
    """Read one line through the single stdin reader; None on EOF.

    The reader thread wakes this coroutine through a thread-safe callback, so
    no AnyIO worker is parked on ``done.wait()``. KeyboardInterrupt or
    cancellation at the prompt propagates to the caller after marking the
    in-flight read abandoned, so its line is discarded rather than corrupting
    the next prompt — and no worker is left blocked to stall exit.
    """
    import asyncio

    while True:
        console.print(Text(prompt), end="")
        request = _stdin_reader.submit()
        ready = anyio.Event()
        loop = asyncio.get_running_loop()
        request.wake = lambda: loop.call_soon_threadsafe(ready.set)
        # Close the race where the reader resolved before ``wake`` was set.
        if request.done.is_set():
            ready.set()
        try:
            await ready.wait()
        except BaseException:
            request.abandoned = True
            raise
        if request.line is _DECODE_ERROR:
            console.print(
                Text(
                    "Could not decode input as UTF-8; please try again.",
                    style="yellow",
                )
            )
            continue
        return request.line if request.line is None else str(request.line)
