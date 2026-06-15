"""Rich rendering of a streaming agent run.

``EventRenderer`` consumes runtime events delivered through ``RunSession`` live
observation and turns them into a Claude Code style terminal view:
a thinking spinner, streamed assistant text, and live tool-call lines.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from knuth.core.events import RuntimeEvent
from knuth.core.messages import ToolCall
from knuth_runtime.observation import RuntimeEventInterest, RuntimeEventOverflowPolicy
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from knuth_toold.process_output import TaggedProcessOutput, parse_tagged_process_output

_MAX_ARG_LEN = 80
_MAX_RESULT_LEN = 400
_MAX_REASONING_TAIL = 80
_MAX_TOOL_PREVIEW_LINES = 6


class EventRenderer:
    interest = RuntimeEventInterest.for_prefixes(
        "model.",
        "tool.",
        "approval.",
        "run.invocation.",
    )
    required = True
    buffer_size = 1000
    overflow_policy = RuntimeEventOverflowPolicy.BLOCK

    def __init__(self, console: Console) -> None:
        self._console = console
        self._thinking: Live | None = None
        self._thinking_spinner: Spinner | None = None
        self._thinking_started_at: float | None = None
        self._reasoning_parts: list[str] = []
        self._content_live: Live | None = None
        self._content_parts: list[str] = []

        self._tool_names: dict[str, str] = {}
        self._dispatch: dict[str, Callable[[Any], None]] = {
            "model.reasoning.delta": self._on_reasoning_delta,
            "model.reasoning.completed": self._on_reasoning_completed,
            "model.content.delta": self._on_content_delta,
            "model.tool_call.completed": self._on_tool_call_completed,
            "model.completed": self._on_model_completed,
            "model.failed": self._on_model_failed,
            "model.aborted": self._on_model_aborted,
            "tool.batch_planned": self._on_batch_planned,
            "tool.invocation_started": self._on_tool_started,
            "tool.invocation_completed": self._on_tool_completed,
            "approval.requested": self._on_approval_requested,
        }

    async def handle_event(self, event: RuntimeEvent) -> None:
        handler = self._dispatch.get(event.type)
        if handler is not None:
            handler(event)

    def remember_tool_names(self, tool_names: dict[str, str]) -> None:
        for tool_call_id, name in tool_names.items():
            if tool_call_id and name:
                self._tool_names[tool_call_id] = name

    def finish(self) -> None:
        """Tear down any live region left open (end of a turn)."""
        self._stop_thinking()
        self._stop_content()

    # -- event handlers ----------------------------------------------------

    def _on_reasoning_delta(self, event: Any) -> None:
        # Keep accumulated answer text buffered across interleaved reasoning;
        # flushing here would split streamed markdown into broken fragments.
        self._suspend_content()
        self._start_thinking()
        self._append_reasoning(event.delta)

    def _on_reasoning_completed(self, _event: Any) -> None:
        self._stop_thinking()

    def _on_content_delta(self, event: Any) -> None:
        self._stop_thinking()
        self._append_content(event.delta)

    def _on_tool_call_completed(self, event: Any) -> None:
        self._stop_thinking()
        self._stop_content()
        self._print_tool_call(event.tool_call)

    def _on_model_completed(self, _event: Any) -> None:
        self._stop_thinking()
        self._stop_content()

    def _on_model_failed(self, event: Any) -> None:
        self._stop_thinking()
        self._stop_content()
        self._console.print(Text(f"✗ error: {event.error.message}", style="bold red"))

    def _on_model_aborted(self, _event: Any) -> None:
        self._stop_thinking()
        self._stop_content()
        self._console.print(Text("⊘ aborted", style="yellow"))

    def _on_batch_planned(self, event: Any) -> None:
        for call in event.calls:
            self._tool_names[call.tool_call_id] = call.name

    def _on_tool_started(self, event: Any) -> None:
        self._stop_thinking()
        self._stop_content()
        name = self._tool_names.get(event.tool_call_id, event.tool_call_id)
        self._console.print(Text(f"  ⏳ {name}…", style="dim"))

    def _on_tool_completed(self, event: Any) -> None:
        self._print_tool_completed(event)

    def _on_approval_requested(self, event: Any) -> None:
        self._stop_thinking()
        self._stop_content()
        title = event.title or event.reason or "tool call"
        suffix = f" [risk: {event.risk}]" if event.risk else ""
        self._console.print(
            Text(f"  ⚠ approval required: {title}{suffix}", style="yellow")
        )

    # -- thinking spinner -------------------------------------------------

    def _start_thinking(self) -> None:
        if self._thinking_started_at is not None:
            return
        self._thinking_started_at = time.monotonic()
        if not self._console.is_terminal:
            return  # no animation off a real terminal; summary still prints on stop
        self._thinking_spinner = Spinner("dots", text=Text(" Thinking…", style="dim"))
        self._thinking = Live(
            self._thinking_spinner,
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._thinking.start()

    def _append_reasoning(self, delta: str) -> None:
        if not delta:
            return
        self._reasoning_parts.append(delta)
        if self._thinking_spinner is not None:
            tail = "".join(self._reasoning_parts)[-_MAX_REASONING_TAIL:]
            tail = " ".join(tail.split())
            self._thinking_spinner.update(
                text=Text(f" Thinking… {tail}", style="dim")
            )

    def _stop_thinking(self) -> None:
        if self._thinking_started_at is None:
            return
        if self._thinking is not None:
            self._thinking.stop()
            self._thinking = None
        self._thinking_spinner = None
        elapsed = time.monotonic() - self._thinking_started_at
        self._thinking_started_at = None
        self._reasoning_parts = []
        self._console.print(Text(f"✶ thought for {elapsed:.1f}s", style="dim italic"))

    # -- streamed assistant text ------------------------------------------

    def _append_content(self, delta: str) -> None:
        if not delta:
            return
        self._content_parts.append(delta)
        if not self._console.is_terminal:
            return  # accumulate; flushed as one block in _stop_content
        if self._content_live is None:
            self._content_live = Live(
                self._render_content(),
                console=self._console,
                refresh_per_second=12,
                transient=True,
            )
            self._content_live.start()
        else:
            self._content_live.update(self._render_content())

    def _render_content(self):
        return Text("".join(self._content_parts))

    def _suspend_content(self) -> None:
        """Hide the live answer region (e.g. while thinking) but keep the buffer."""
        if self._content_live is not None:
            self._content_live.stop()
            self._content_live = None

    def _stop_content(self) -> None:
        if not self._content_parts:
            self._suspend_content()
            return
        text = "".join(self._content_parts)
        self._content_parts = []
        self._suspend_content()
        if not text.strip():
            return  # whitespace-only content is model noise, not an answer
        self._console.print(Markdown(text))

    # -- tool rendering ---------------------------------------------------

    def _print_tool_call(self, tool_call: ToolCall) -> None:
        name = tool_call.name or "tool"
        args = tool_call.arguments
        self._console.print(Text(f"● {name}({_format_args(args)})", style="bold blue"))

    def _print_tool_completed(self, event: Any) -> None:
        self._stop_thinking()
        self._stop_content()
        name = event.tool_name
        if event.outcome == "denied":
            self._console.print(Text(f"  ✘ {name} denied", style="red"))
            return
        ok = event.outcome == "succeeded"
        body = event.observation or event.observation_preview or ""
        if name == "shell":
            parsed = parse_tagged_process_output(str(body))
            if parsed is not None:
                self._print_shell_completed(parsed, ok)
                return
        mark = "✔" if ok else "✘"
        style = "green" if ok else "red"
        preview = _format_tool_preview(str(body))
        line = f"  {mark} {name}"
        if preview.inline:
            line += f" — {preview.inline}"
        self._console.print(Text(line, style=style))
        for preview_line in preview.lines:
            self._console.print(Text(f"    {preview_line}", style="dim"))

    def _print_shell_completed(self, output: TaggedProcessOutput, ok: bool) -> None:
        mark = "✔" if ok else "✘"
        style = "green" if ok else "red"
        self._console.print(
            Text(f"  {mark} shell exit {output.return_code}", style=style)
        )
        if output.stdout:
            self._console.print(Text("    stdout:", style="dim"))
            self._console.print(Text(_truncate(output.stdout.rstrip(), _MAX_RESULT_LEN)))
        if output.stderr:
            self._console.print(Text("    stderr:", style="dim"))
            self._console.print(
                Text(_truncate(output.stderr.rstrip(), _MAX_RESULT_LEN), style="yellow")
            )
        if output.offload.get("status") == "offloaded":
            self._console.print(Text("    offload:", style="dim"))
            for label in ("stdout", "stderr"):
                payload = output.offload.get(label)
                if isinstance(payload, dict) and payload.get("path"):
                    self._console.print(
                        Text(f"      {label}: {payload['path']}", style="dim")
                    )
            result_path = output.offload.get("result_path")
            if result_path:
                self._console.print(Text(f"      result: {result_path}", style="dim"))


def _format_args(args: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(args)
    return _truncate(rendered, _MAX_ARG_LEN)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class _ToolPreview:
    def __init__(self, *, inline: str = "", lines: list[str] | None = None) -> None:
        self.inline = inline
        self.lines = lines or []


def _format_tool_preview(body: str) -> _ToolPreview:
    text = body.strip()
    if not text:
        return _ToolPreview()
    lines = text.splitlines()
    if len(lines) <= 1:
        return _ToolPreview(inline=_truncate(text.replace("\n", " "), _MAX_RESULT_LEN))

    visible = lines[:_MAX_TOOL_PREVIEW_LINES]
    rendered = [_truncate(line, _MAX_RESULT_LEN) for line in visible]
    hidden = len(lines) - len(visible)
    if hidden > 0:
        rendered.append(f"… {hidden} more lines")
    return _ToolPreview(lines=rendered)
