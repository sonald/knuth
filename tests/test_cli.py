import contextlib
import io
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from knuth.core.events import (
    ModelContentDeltaDraft,
    RunSucceeded,
    emit_transient_runtime_event,
)
from knuth.core.types import RunStatus
from knuth_cli.cli import main
from knuth_runtime import RunResult


class _StreamingFakeRuntime:
    """Fake runtime that emits a content stream for ``run_streaming``."""

    async def run_streaming(self, prompt, on_event, *, run_id=None) -> RunResult:
        answer = f"real-ish: {prompt}"
        await on_event(
            emit_transient_runtime_event(
                run_id or "run-1",
                ModelContentDeltaDraft(delta=answer),
                event_id="evt-1",
                created_at="2026-06-05T00:00:00Z",
            )
        )
        return RunResult(answer=answer, run_id=run_id or "run-1", status=RunStatus.SUCCEEDED)


class CliTests(unittest.TestCase):
    def test_run_once_streams_answer_to_stdout(self) -> None:
        output = io.StringIO()

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with contextlib.redirect_stdout(output):
            exit_code = main(
                ["run", "--once", "hello"],
                runtime_factory=runtime_factory,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("real-ish: hello", output.getvalue())

    def test_interactive_run_loop_stays_in_cli_layer(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("hello\n/exit\n")

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("Knuth agent ready", output.getvalue())
        self.assertIn("real-ish: hello", output.getvalue())

    def test_run_help_does_not_expose_workspace_option(self) -> None:
        output = io.StringIO()

        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["run", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("workspace", output.getvalue())

    def test_status_events_tools_and_approval_commands_call_runtime(self) -> None:
        @dataclass
        class FakeApproval:
            id: str
            status: object

        class FakeRuntime:
            async def status(self, run_id: str) -> RunStatus:
                return RunStatus.SUCCEEDED

            async def events(self, run_id: str):
                return [
                    RunSucceeded(
                        id="evt-1",
                        run_id=run_id,
                        seq=1,
                        type="run.succeeded",
                        answer="ok",
                        turns=1,
                        created_at="2026-06-05T00:00:00Z",
                    )
                ]

            async def tools(self):
                return [
                    {
                        "type": "function",
                        "function": {"name": "read_file", "description": "Read"},
                    }
                ]

            async def approve(self, approval_id: str):
                return FakeApproval(approval_id, RunStatus.SUCCEEDED)

            async def deny(self, approval_id: str):
                return FakeApproval(approval_id, RunStatus.CANCELLED)

            async def resume(self, run_id: str) -> RunResult:
                return RunResult(
                    answer="resumed",
                    run_id=run_id,
                    status=RunStatus.SUCCEEDED,
                )

        async def runtime_factory() -> FakeRuntime:
            return FakeRuntime()

        for argv, expected in [
            (["status", "run-1"], "succeeded"),
            (["events", "run-1"], "run.succeeded"),
            (["tools", "list"], "read_file"),
            (["approve", "appr-1"], "appr-1"),
            (["deny", "appr-1"], "appr-1"),
            (["resume", "run-1"], "resumed"),
        ]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(argv, runtime_factory=runtime_factory)
            self.assertEqual(exit_code, 0)
            self.assertIn(expected, output.getvalue())


if __name__ == "__main__":
    unittest.main()
