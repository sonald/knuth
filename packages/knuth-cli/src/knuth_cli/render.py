"""Rich rendering of a streaming agent run.

``EventRenderer`` consumes the events forwarded by ``run_agent_loop`` via the
``on_event`` callback and turns them into a Claude Code style terminal view:
a thinking spinner, streamed assistant text, and live tool-call lines.

Two event shapes flow through ``handle``:

- ``InferenceEvent`` (token-level): ``generation_start`` / ``content_delta`` /
  ``reasoning_delta`` / ``tool_call`` / ``generation_end`` / ``error`` / ...
- ``RuntimeEvent`` (durable lifecycle): ``tool/started`` / ``tool/completed`` /
  ``approval/requested`` / ``user_input/requested`` / ...
"""

from __future__ import annotations

import json
import time
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

_MAX_ARG_LEN = 80
_MAX_RESULT_LEN = 400
_MAX_REASONING_TAIL = 80


class EventRenderer:
    def __init__(self, console: Console) -> None:
        self._console = console
        self._thinking: Live | None = None
        self._thinking_spinner: Spinner | None = None
        self._thinking_started_at: float | None = None
        self._reasoning_parts: list[str] = []
        self._content_live: Live | None = None
        self._content_parts: list[str] = []

    async def handle(self, event: Any) -> None:
        # RuntimeEvent has namespace/name; InferenceEvent has a `.type`.
        if getattr(event, "namespace", None) is not None:
            self._handle_runtime_event(event)
        else:
            self._handle_inference_event(event)

    def finish(self) -> None:
        """Tear down any live region left open (end of a turn)."""
        self._stop_thinking()
        self._stop_content()

    # -- inference (token-level) ------------------------------------------

    def _handle_inference_event(self, event: Any) -> None:
        etype = str(getattr(event, "type", ""))
        payload = getattr(event, "payload", {}) or {}
        if etype == "reasoning_delta":
            self._stop_content()
            self._start_thinking()
            self._append_reasoning(str(payload.get("delta", "")))
        elif etype == "content_delta":
            self._stop_thinking()
            self._append_content(str(payload.get("delta", "")))
        elif etype == "tool_call":
            self._stop_thinking()
            self._stop_content()
            self._print_tool_call(payload.get("tool_call") or {})
        elif etype == "generation_end":
            self._stop_thinking()
            self._stop_content()
        elif etype == "error":
            self._stop_thinking()
            self._stop_content()
            message = payload.get("message") or payload.get("code") or "unknown error"
            self._console.print(Text(f"✗ error: {message}", style="bold red"))
        elif etype == "aborted":
            self._stop_thinking()
            self._stop_content()
            self._console.print(Text("⊘ aborted", style="yellow"))

    # -- runtime (lifecycle) ----------------------------------------------

    def _handle_runtime_event(self, event: Any) -> None:
        key = (event.namespace, event.name)
        payload = event.payload or {}
        if key == ("tool", "started"):
            self._stop_thinking()
            self._stop_content()
            name = (payload.get("intent") or {}).get("name", "tool")
            self._console.print(Text(f"  ⏳ {name}…", style="dim"))
        elif key == ("tool", "completed"):
            self._print_tool_completed(payload)
        elif key == ("approval", "requested"):
            self._stop_thinking()
            self._stop_content()
            title = payload.get("title") or payload.get("reason") or "tool call"
            risk = payload.get("risk")
            suffix = f" [risk: {risk}]" if risk else ""
            self._console.print(
                Text(f"  ⚠ approval required: {title}{suffix}", style="yellow")
            )
        elif key == ("user_input", "requested"):
            self._stop_thinking()
            self._stop_content()
            question = payload.get("question") or "(no question)"
            self._console.print(Text(f"  ? {question}", style="cyan"))

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
            )
            self._content_live.start()
        else:
            self._content_live.update(self._render_content())

    def _render_content(self):
        return Text("".join(self._content_parts))

    def _stop_content(self) -> None:
        if not self._content_parts:
            return
        text = "".join(self._content_parts)
        renderable = Markdown(text) if text.strip() else Text(text)
        if self._content_live is not None:
            self._content_live.update(renderable)
            self._content_live.stop()
            self._content_live = None
        else:
            self._console.print(renderable)
        self._content_parts = []

    # -- tool rendering ---------------------------------------------------

    def _print_tool_call(self, tool_call: dict[str, Any]) -> None:
        name = tool_call.get("name", "tool")
        args = tool_call.get("arguments") or {}
        self._console.print(Text(f"● {name}({_format_args(args)})", style="bold blue"))

    def _print_tool_completed(self, payload: dict[str, Any]) -> None:
        self._stop_thinking()
        self._stop_content()
        name = (payload.get("intent") or {}).get("name", "tool")
        if payload.get("denied"):
            self._console.print(Text(f"  ✘ {name} denied", style="red"))
            return
        result = payload.get("result") or {}
        ok = result.get("ok", True)
        body = result.get("content") or result.get("error") or ""
        mark = "✔" if ok else "✘"
        style = "green" if ok else "red"
        summary = _truncate(str(body).strip().replace("\n", " "), _MAX_RESULT_LEN)
        line = f"  {mark} {name}"
        if summary:
            line += f" — {summary}"
        self._console.print(Text(line, style=style))


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
