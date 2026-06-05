import contextlib
import io
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from knuth.core.events import RuntimeEvent
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.types import RunStatus
from knuth_cli.cli import main
from knuth_runtime import AgentTurn


class CliTests(unittest.TestCase):
    def test_run_once_calls_injected_runtime_factory_without_workspace(self) -> None:
        output = io.StringIO()

        class FakeRuntime:
            async def run_once(self, prompt: str) -> AgentTurn:
                return AgentTurn(
                    answer=f"real-ish: {prompt}",
                    messages=(
                        InferenceMessage(role=InferenceRole.ASSISTANT, content="ok"),
                    ),
                    tool_calls=(),
                )

        async def runtime_factory() -> FakeRuntime:
            return FakeRuntime()

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

        class FakeRuntime:
            async def run_once(self, prompt: str) -> AgentTurn:
                return AgentTurn(
                    answer=f"repl: {prompt}",
                    messages=(),
                    tool_calls=(),
                )

        async def runtime_factory() -> FakeRuntime:
            return FakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("Knuth agent ready", output.getvalue())
        self.assertIn("repl: hello", output.getvalue())

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
                    RuntimeEvent(
                        id="evt-1",
                        run_id=run_id,
                        seq=1,
                        namespace="run",
                        name="succeeded",
                        type="run.succeeded",
                        payload={"answer": "ok"},
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

            async def resume(self, run_id: str) -> AgentTurn:
                return AgentTurn(
                    answer="resumed",
                    messages=(),
                    tool_calls=(),
                    run_id=run_id,
                    status=RunStatus.SUCCEEDED,
                )

        async def runtime_factory() -> FakeRuntime:
            return FakeRuntime()

        for argv, expected in [
            (["status", "run-1"], "succeeded"),
            (["events", "run-1"], "run.succeeded"),
            (["tools", "list"], "read_file"),
            (["tools", "refresh"], "read_file"),
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
