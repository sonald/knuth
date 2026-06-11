"""Interactive, Claude Code style REPL for the Knuth agent."""

from __future__ import annotations

import queue
import signal
import sys
import threading
from dataclasses import dataclass, field

import anyio
from knuth_runtime import AgentRuntime, RunResult, RuntimeObservationError
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


class _TurnInterrupted(Exception):
    """The user pressed Ctrl-C while a turn was in flight."""

    def __init__(self, run_id: str | None) -> None:
        super().__init__("turn interrupted")
        self.run_id = run_id


async def run_interactive(runtime: AgentRuntime, console: Console) -> int:
    console.print(Text(_BANNER, style="bold"))
    session_run_id: str | None = None
    allowed_tools: set[str] = set()
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
            session_run_id = await _run_turn_interruptible(
                runtime, console, prompt, session_run_id, allowed_tools
            )
        except _TurnInterrupted as interrupt:
            session_run_id = await _pause_after_interrupt(
                runtime, console, interrupt.run_id or session_run_id
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
        run_id, result = await _run_turn_with_result(
            runtime, console, prompt, None, set()
        )
    except _TurnInterrupted as interrupt:
        await _pause_after_interrupt(runtime, console, interrupt.run_id)
        return _EXIT_INTERRUPTED
    except Exception as exc:
        console.print(
            Text(f"Run failed: {exc.__class__.__name__}: {exc}", style="bold red")
        )
        return 1
    if result is None:
        return 1
    status = result.status
    footer = f"run {run_id} · {status.value if status else 'unknown'}"
    console.print(Text(footer, style="dim"))
    if status == RunStatus.SUCCEEDED:
        return 0
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
    renderer = EventRenderer(console)
    try:
        async with runtime.resume(run_id, listeners=[renderer]) as session:
            result = await session.result()
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


async def _run_turn_interruptible(
    runtime: AgentRuntime,
    console: Console,
    prompt: str,
    session_run_id: str | None,
    allowed_tools: set[str],
) -> str | None:
    run_id, _ = await _run_turn_with_result(
        runtime, console, prompt, session_run_id, allowed_tools
    )
    return run_id


async def _run_turn_with_result(
    runtime: AgentRuntime,
    console: Console,
    prompt: str,
    session_run_id: str | None,
    allowed_tools: set[str],
) -> tuple[str | None, RunResult | None]:
    """Run one turn, cancelling it (instead of dying) on Ctrl-C.

    While the turn is in flight SIGINT cancels the turn's scope; the run is
    then marked paused by the caller so it can be resumed.
    """
    observed_run_id: list[str | None] = [session_run_id]
    result_holder: list[RunResult | None] = [None]
    try:
        with anyio.CancelScope() as turn_scope:
            async with anyio.create_task_group() as tg:

                async def _watch_sigint() -> None:
                    with anyio.open_signal_receiver(signal.SIGINT) as signals:
                        async for _ in signals:
                            turn_scope.cancel()
                            return

                tg.start_soon(_watch_sigint)
                try:
                    observed_run_id[0], result_holder[0] = await _run_turn(
                        runtime,
                        console,
                        prompt,
                        session_run_id,
                        allowed_tools,
                        observed_run_id,
                    )
                finally:
                    tg.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise
    if turn_scope.cancelled_caught:
        raise _TurnInterrupted(observed_run_id[0])
    return observed_run_id[0], result_holder[0]


async def _pause_after_interrupt(
    runtime: AgentRuntime, console: Console, run_id: str | None
) -> str | None:
    console.print(Text("⊘ interrupted", style="yellow"))
    if run_id is not None:
        try:
            status = await runtime.pause(run_id)
        except Exception:
            return run_id
        console.print(
            Text(f"run {run_id} · {status.value} (resume with /new message or `knuth resume`)", style="dim")
        )
    return run_id


async def _run_turn(
    runtime: AgentRuntime,
    console: Console,
    prompt: str,
    session_run_id: str | None,
    allowed_tools: set[str],
    observed_run_id: list[str | None] | None = None,
) -> tuple[str | None, RunResult | None]:
    renderer = EventRenderer(console)
    session_factory = (
        runtime.start(prompt, listeners=[renderer])
        if session_run_id is None
        else runtime.continue_run(session_run_id, prompt, listeners=[renderer])
    )
    try:
        async with session_factory as session:
            if observed_run_id is not None:
                observed_run_id[0] = getattr(session, "run_id", session_run_id)
            result = await session.result()
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
    delivering it to a caller that already gave up (Ctrl-C, cancellation)."""

    done: threading.Event = field(default_factory=threading.Event)
    line: object = None
    abandoned: bool = False

    def resolve(self, line: object) -> None:
        self.line = line
        self.done.set()


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

    KeyboardInterrupt from Ctrl-C at the prompt propagates to the caller;
    the in-flight read is marked abandoned so its line is discarded rather
    than corrupting the next prompt.
    """
    while True:
        console.print(Text(prompt), end="")
        request = _stdin_reader.submit()
        try:
            await anyio.to_thread.run_sync(
                request.done.wait, abandon_on_cancel=True
            )
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
