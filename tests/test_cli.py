import contextlib
import io
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import anyio
import platformdirs

from knuth.core.events import (
    InferenceGenerationCompleted,
    ModelContentDeltaDraft,
    RunSucceeded,
    emit_transient_runtime_event,
)
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.types import RunStatus
from knuth_cli.cli import main
from knuth_cli.config import AgentConfig, default_config_path, load_config
from knuth_cli.runtime import build_runtime
from knuth_runtime import RunResult


def _write_yaml(path: Path, values: dict[str, object]) -> None:
    lines = []
    for key, value in values.items():
        if isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines), encoding="utf-8")


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


class CapturingInferenceClient:
    model = "capturing-model"
    instances: list["CapturingInferenceClient"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.captured_messages: list[list[InferenceMessage]] = []
        self.instances.append(self)

    async def stream(self, messages, tools, config, runtime=None):
        self.captured_messages.append(list(messages))
        yield InferenceGenerationCompleted(
            generation_id="gen-1",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT,
                content="ok",
            ),
        )


class AgentConfigTests(unittest.TestCase):
    def test_default_config_path_lives_in_cli_agent_config_dir(self) -> None:
        path = default_config_path()

        expected_parent = Path(platformdirs.user_data_dir("knuth")) / "knuth-cli"
        self.assertEqual(path.parent, expected_parent)
        self.assertEqual(path.name, "knuth.yaml")

    def test_load_config_reads_yaml_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            _write_yaml(
                config_path,
                {
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                    "timeout": 45.5,
                    "system_prompt": "You are Knuth.",
                },
            )

            config = anyio.run(load_config, config_path, {})

            self.assertIsInstance(config, AgentConfig)
            self.assertEqual(config.api_key, "test-key")
            self.assertEqual(config.base_url, "https://example.test/v1")
            self.assertEqual(config.model, "test-model")
            self.assertEqual(config.timeout, 45.5)
            self.assertEqual(config.system_prompt, "You are Knuth.")

    def test_environment_values_override_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            _write_yaml(
                config_path,
                {
                    "api_key": "file-key",
                    "base_url": "https://file.test/v1",
                    "model": "file-model",
                    "timeout": 30,
                },
            )

            config = anyio.run(
                load_config,
                config_path,
                {
                    "KNUTH_API_KEY": "env-key",
                    "KNUTH_BASE_URL": "https://env.test/v1",
                    "KNUTH_MODEL": "env-model",
                    "KNUTH_TIMEOUT": "90.5",
                    "KNUTH_SYSTEM_PROMPT": "env prompt",
                },
            )

            self.assertEqual(config.api_key, "env-key")
            self.assertEqual(config.base_url, "https://env.test/v1")
            self.assertEqual(config.model, "env-model")
            self.assertEqual(config.timeout, 90.5)
            self.assertEqual(config.system_prompt, "env prompt")

    def test_load_config_fails_when_required_values_are_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "KNUTH_API_KEY"):
            anyio.run(load_config, Path("does-not-exist.yaml"), {})

    def test_load_config_defaults_to_user_data_dir_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth-cli", "knuth.yaml")
            config_path.parent.mkdir(parents=True)
            _write_yaml(
                config_path,
                {
                    "api_key": "default-key",
                    "base_url": "https://default.test/v1",
                    "model": "default-model",
                },
            )

            with patch(
                "knuth_cli.config.default_config_path",
                return_value=config_path,
            ):
                config = anyio.run(load_config, None, {})

            self.assertEqual(config.api_key, "default-key")
            self.assertEqual(config.base_url, "https://default.test/v1")
            self.assertEqual(config.model, "default-model")
            self.assertIsNone(config.system_prompt)


class CliRuntimeFactoryTests(unittest.TestCase):
    def test_build_runtime_injects_user_system_prompt_as_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            db_path = Path(temp_dir, "knuth.db")
            _write_yaml(
                config_path,
                {
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                    "system_prompt": "USER PROMPT",
                },
            )

            CapturingInferenceClient.instances.clear()

            async def make_runtime():
                return await build_runtime(config_path=config_path, db_path=db_path)

            with patch(
                "knuth_cli.runtime.LiteLLMInferenceClient",
                CapturingInferenceClient,
            ):
                runtime = anyio.run(make_runtime)
                result = anyio.run(runtime.run_once, "hi")

        client = CapturingInferenceClient.instances[0]
        self.assertEqual(result.answer, "ok")
        self.assertEqual(client.kwargs["model"], "test-model")
        first_turn_messages = client.captured_messages[0]
        self.assertEqual(first_turn_messages[0].role, InferenceRole.SYSTEM)
        self.assertIn("local agent runtime", first_turn_messages[0].content or "")
        self.assertIn("USER PROMPT", first_turn_messages[0].content or "")


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
