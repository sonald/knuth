from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, ListItem
from textual.widgets import ListView, RichLog, Static

from knuth_cli.runtime_event_tui.controller import RuntimeEventTuiController
from knuth_cli.runtime_event_tui.models import ObservedEventRow, RunSnapshot
from knuth_cli.runtime_event_tui.views import (
    event_detail_text,
    event_matches_filter,
    event_row_label,
    latest_system_preamble,
    run_snapshot_text,
)


class RuntimeEventTui(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #run-bar {
        height: 3;
        layout: horizontal;
        padding: 0 1;
    }

    #prompt-input {
        width: 2fr;
        margin-right: 1;
    }

    #run-id-input {
        width: 28;
        margin-right: 1;
    }

    #filter-input {
        width: 24;
        margin-right: 1;
    }

    #run-button, #resume-button, #load-button {
        width: 10;
        margin-right: 1;
    }

    #status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #body {
        height: 1fr;
        layout: horizontal;
        padding: 0 1 1 1;
    }

    #event-list {
        width: 45;
        border: solid $accent;
        margin-right: 1;
    }

    #detail {
        width: 1fr;
        border: solid $accent;
        margin-right: 1;
    }

    #inspector {
        width: 46;
        border: solid $accent;
    }
    """

    BINDINGS = [
        ("ctrl+r", "run_prompt", "Run"),
        ("ctrl+l", "load_history", "Load"),
        ("ctrl+f", "focus_filter", "Filter"),
        ("a", "approve", "Approve"),
        ("d", "deny", "Deny"),
        ("r", "resume", "Resume"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        controller: RuntimeEventTuiController,
        *,
        initial_prompt: str = "",
        initial_run_id: str = "",
    ) -> None:
        super().__init__()
        self.controller = controller
        self.controller.set_live_row_callback(self._on_live_row)
        self.initial_prompt = initial_prompt
        self.initial_run_id = initial_run_id
        self._rows: list[ObservedEventRow] = []
        self._visible_rows: list[ObservedEventRow] = []
        self._snapshot: RunSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            with Horizontal(id="run-bar"):
                yield Input(
                    value=self.initial_prompt,
                    placeholder="Prompt",
                    id="prompt-input",
                )
                yield Input(
                    value=self.initial_run_id,
                    placeholder="run_id",
                    id="run-id-input",
                )
                yield Input(value="all", placeholder="filter", id="filter-input")
                yield Button("Run", variant="primary", id="run-button")
                yield Button("Resume", id="resume-button")
                yield Button("Load", id="load-button")
            yield Static("ready", id="status")
            with Horizontal(id="body"):
                yield ListView(id="event-list")
                yield RichLog(id="detail", wrap=True, markup=False, highlight=True)
                yield RichLog(id="inspector", wrap=True, markup=False, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()
        self._render_detail(None)
        self._render_inspector()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-button":
            self.run_worker(self._run_current_prompt(), exclusive=True)
        elif event.button.id == "resume-button":
            self.run_worker(self._resume_current_run(), exclusive=True)
        elif event.button.id == "load-button":
            self.run_worker(self._load_current_history(), exclusive=True)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "prompt-input":
            self.run_worker(self._run_current_prompt(), exclusive=True)
        elif event.input.id in {"run-id-input", "filter-input"}:
            await self._load_or_refresh()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-input":
            await self._refresh_event_list()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        index = self._index_for_item(event.item)
        if index is not None and index < len(self._visible_rows):
            self._render_detail(self._visible_rows[index])

    async def action_run_prompt(self) -> None:
        self.run_worker(self._run_current_prompt(), exclusive=True)

    async def action_load_history(self) -> None:
        self.run_worker(self._load_current_history(), exclusive=True)

    async def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    async def action_resume(self) -> None:
        self.run_worker(self._resume_current_run(), exclusive=True)

    async def action_approve(self) -> None:
        await self._resolve_first_approval("approve")

    async def action_deny(self) -> None:
        await self._resolve_first_approval("deny")

    async def _run_current_prompt(self) -> None:
        prompt = self.query_one("#prompt-input", Input).value
        run_id = self.query_one("#run-id-input", Input).value.strip() or None
        self._rows = []
        await self._refresh_event_list()
        await self._with_status("running", self.controller.start(prompt, run_id=run_id))

    async def _resume_current_run(self) -> None:
        run_id = self.query_one("#run-id-input", Input).value.strip() or None
        await self._with_status("resuming", self.controller.resume(run_id))

    async def _load_current_history(self) -> None:
        run_id = self.query_one("#run-id-input", Input).value.strip()
        if not run_id:
            self._set_status("enter a run_id")
            return
        await self._with_status("loading", self.controller.load_history(run_id))

    async def _load_or_refresh(self) -> None:
        if self.query_one("#run-id-input", Input).value.strip():
            await self._load_current_history()
        else:
            await self._refresh_event_list()

    async def _resolve_first_approval(self, action: str) -> None:
        if self._snapshot is None or not self._snapshot.approvals:
            self._set_status("no pending approval")
            return
        approval_id = self._snapshot.approvals[0].approval_id
        operation = (
            self.controller.approve(approval_id)
            if action == "approve"
            else self.controller.deny(approval_id)
        )
        await self._with_status(action, operation)

    async def _with_status(self, message: str, operation) -> None:
        self._set_status(message)
        try:
            snapshot = await operation
        except Exception as exc:
            self._set_status(f"error: {exc}")
            return
        await self._apply_snapshot(snapshot)
        self._set_status(snapshot.status or "loaded")

    async def _on_live_row(self, row: ObservedEventRow) -> None:
        self._rows.append(row)
        await self._append_visible_row(row)
        if row.event_type == "context.system_preamble.built":
            self._render_inspector()

    async def _apply_snapshot(self, snapshot: RunSnapshot) -> None:
        self._snapshot = snapshot
        self._rows = list(snapshot.events)
        if snapshot.run_id:
            self.query_one("#run-id-input", Input).value = snapshot.run_id
        await self._refresh_event_list()
        self._render_inspector()

    async def _refresh_event_list(self) -> None:
        event_list = self.query_one("#event-list", ListView)
        await event_list.clear()
        self._visible_rows = []
        for row in self._rows:
            await self._append_visible_row(row)

    async def _append_visible_row(self, row: ObservedEventRow) -> None:
        query = self.query_one("#filter-input", Input).value
        if not event_matches_filter(row, query):
            return
        self._visible_rows.append(row)
        event_list = self.query_one("#event-list", ListView)
        await event_list.append(ListItem(Label(event_row_label(row))))
        event_list.index = len(self._visible_rows) - 1
        event_list.scroll_end(animate=False, immediate=True)
        self._render_detail(row)

    def _render_detail(self, row: ObservedEventRow | None) -> None:
        detail = self.query_one("#detail", RichLog)
        detail.clear()
        detail.write(event_detail_text(row))

    def _render_inspector(self) -> None:
        inspector = self.query_one("#inspector", RichLog)
        inspector.clear()
        snapshot = self._snapshot
        if snapshot is None and self._rows:
            snapshot = RunSnapshot(
                run_id=self.controller.current_run_id,
                status="running",
                events=tuple(self._rows),
                latest_system_preamble=latest_system_preamble(self._rows),
            )
        inspector.write(run_snapshot_text(snapshot))

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _index_for_item(self, item: ListItem) -> int | None:
        event_list = self.query_one("#event-list", ListView)
        for index, child in enumerate(event_list.children):
            if child is item:
                return index
        return None
