from __future__ import annotations

from types import SimpleNamespace
import unittest

import anyio
from textual.widgets import Input, ListView, RichLog

from knuth.core.events import (
    ContextSystemPreambleBuiltDraft,
    RunCreatedDraft,
    StepStartedDraft,
    emit_transient_runtime_event,
    store_runtime_event,
)
from knuth.core.runtime_events import ContextSnapshot
from knuth.core.types import RunStatus

from knuth_cli.runtime_event_tui.app import RuntimeEventTui
from knuth_cli.runtime_event_tui.capture import RuntimeEventCapture
from knuth_cli.runtime_event_tui.controller import RuntimeEventTuiController
from knuth_cli.runtime_event_tui.models import ObservedEventRow
from knuth_cli.runtime_event_tui.views import (
    dedupe_event_rows,
    event_detail_text,
    event_matches_filter,
    event_row_label,
    latest_system_preamble,
)


def _preamble_event(content: str | None = "SYSTEM"):
    return emit_transient_runtime_event(
        "run-1",
        ContextSystemPreambleBuiltDraft(content=content),
        event_id="evt-preamble",
        created_at="2026-06-18T00:00:00Z",
    )


def _durable_event(seq: int = 1):
    return store_runtime_event(
        "run-1",
        seq,
        RunCreatedDraft(query="hello"),
        event_id=f"evt-{seq}",
        created_at="2026-06-18T00:00:00Z",
    )


def _step_event(seq: int = 2):
    return store_runtime_event(
        "run-1",
        seq,
        StepStartedDraft(
            step_id="step-1",
            index=1,
            snapshot=ContextSnapshot(
                messages_hash="m",
                tools_hash="t",
                preamble_hash="p",
                model_config_hash="c",
                message_count=2,
                tool_count=0,
            ),
        ),
        event_id=f"evt-{seq}",
        created_at="2026-06-18T00:00:01Z",
    )


def _live_row(event=None, receive_index: int = 1) -> ObservedEventRow:
    event = event or _preamble_event()
    return ObservedEventRow.from_event(
        event,
        source="live",
        receive_index=receive_index,
    )


class RuntimeEventTuiViewTests(unittest.TestCase):
    def test_event_row_label_shows_source_order_durability_and_type(self) -> None:
        row = _live_row()

        self.assertEqual(
            event_row_label(row),
            "L 001 transient context.system_preamble.built",
        )

    def test_event_detail_text_renders_full_json(self) -> None:
        detail = event_detail_text(_live_row(_preamble_event("SYSTEM TEXT")))

        self.assertIn('"type": "context.system_preamble.built"', detail)
        self.assertIn('"content": "SYSTEM TEXT"', detail)

    def test_event_filter_matches_context_prefix_and_durability(self) -> None:
        transient = _live_row()
        durable = ObservedEventRow.from_event(_durable_event(), source="durable")

        self.assertTrue(event_matches_filter(transient, "context.*"))
        self.assertTrue(event_matches_filter(transient, "transient"))
        self.assertFalse(event_matches_filter(transient, "durable"))
        self.assertTrue(event_matches_filter(durable, "durable"))
        self.assertTrue(event_matches_filter(durable, "run."))

    def test_latest_system_preamble_uses_last_context_event(self) -> None:
        rows = [
            _live_row(_preamble_event("FIRST"), 1),
            _live_row(_preamble_event("SECOND"), 2),
        ]

        self.assertEqual(latest_system_preamble(rows), "SECOND")

    def test_dedupe_event_rows_keeps_live_row_for_same_durable_event(self) -> None:
        event = _durable_event()
        live = ObservedEventRow.from_event(event, source="live", receive_index=1)
        loaded = ObservedEventRow.from_event(event, source="durable")

        self.assertEqual(dedupe_event_rows([live, loaded]), [live])


class RuntimeEventCaptureTests(unittest.TestCase):
    def test_capture_records_receive_order_and_callback(self) -> None:
        async def scenario():
            seen: list[ObservedEventRow] = []

            async def on_row(row: ObservedEventRow) -> None:
                seen.append(row)

            capture = RuntimeEventCapture(on_row=on_row)
            await capture.handle_event(_preamble_event("ONE"))
            await capture.handle_event(_step_event())
            return capture.rows, seen

        rows, seen = anyio.run(scenario)

        self.assertEqual([row.receive_index for row in rows], [1, 2])
        self.assertEqual(seen, list(rows))
        self.assertEqual(rows[0].event_type, "context.system_preamble.built")


class _FakeSession:
    def __init__(self, run_id: str, status: RunStatus) -> None:
        self._result = SimpleNamespace(run_id=run_id, status=status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def result(self):
        return self._result


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.run_id = "run-1"

    def start(self, prompt: str, **kwargs):
        self.calls.append(("start", (prompt,), kwargs))
        return _FakeSession(self.run_id, RunStatus.SUCCEEDED)

    def resume(self, run_id: str, **kwargs):
        self.calls.append(("resume", (run_id,), kwargs))
        return _FakeSession(run_id, RunStatus.SUCCEEDED)

    async def events(self, run_id: str):
        self.calls.append(("events", (run_id,), {}))
        return [_durable_event()]

    async def messages(self, run_id: str):
        self.calls.append(("messages", (run_id,), {}))
        return []

    async def model_context_messages(self, run_id: str):
        self.calls.append(("model_context_messages", (run_id,), {}))
        return []

    async def rewrite_audit(self, run_id: str):
        self.calls.append(("rewrite_audit", (run_id,), {}))
        return []

    async def pending_approvals(self, run_id: str | None = None):
        self.calls.append(("pending_approvals", (run_id,), {}))
        return []

    async def status(self, run_id: str):
        self.calls.append(("status", (run_id,), {}))
        return RunStatus.SUCCEEDED

    async def approve(self, approval_id: str):
        self.calls.append(("approve", (approval_id,), {}))
        return SimpleNamespace(id=approval_id, run_id=self.run_id)

    async def deny(self, approval_id: str):
        self.calls.append(("deny", (approval_id,), {}))
        return SimpleNamespace(id=approval_id, run_id=self.run_id)


class RuntimeEventTuiControllerTests(unittest.TestCase):
    def test_controller_uses_runtime_public_api(self) -> None:
        async def scenario():
            runtime = _FakeRuntime()
            controller = RuntimeEventTuiController(runtime)
            await controller.start("hello")
            await controller.resume("run-1")
            await controller.approve("approval-1")
            await controller.deny("approval-2")
            return [name for name, _, _ in runtime.calls]

        calls = anyio.run(scenario)

        self.assertIn("start", calls)
        self.assertIn("resume", calls)
        self.assertIn("events", calls)
        self.assertIn("messages", calls)
        self.assertIn("model_context_messages", calls)
        self.assertIn("rewrite_audit", calls)
        self.assertIn("pending_approvals", calls)
        self.assertIn("status", calls)
        self.assertIn("approve", calls)
        self.assertIn("deny", calls)


class RuntimeEventTuiAppTests(unittest.TestCase):
    def test_app_mounts_main_workbench_panes(self) -> None:
        async def scenario() -> None:
            app = RuntimeEventTui(RuntimeEventTuiController(_FakeRuntime()))
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.query_one("#prompt-input"), Input)
                self.assertIsInstance(app.query_one("#run-id-input"), Input)
                self.assertIsInstance(app.query_one("#filter-input"), Input)
                self.assertIsInstance(app.query_one("#event-list"), ListView)
                self.assertIsInstance(app.query_one("#detail"), RichLog)
                self.assertIsInstance(app.query_one("#inspector"), RichLog)

        anyio.run(scenario)


if __name__ == "__main__":
    unittest.main()
