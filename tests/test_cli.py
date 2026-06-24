import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import anyio
import platformdirs
from prompt_toolkit.document import Document
from rich.console import Console

from knuth.core.events import (
    InferenceGenerationCompleted,
    ModelContentDeltaDraft,
    RunSucceeded,
    UsageInfo,
    emit_transient_runtime_event,
)
from knuth.core.messages import InferenceMessage, InferenceRole, SystemSectionSource
from knuth.core.skills import SkillInfo, SkillMetadata, SkillSource
from knuth.core.types import RunStatus
from knuth_cli.cli import main
from knuth_cli.completion import (
    CompletionManager,
    CompletionSnapshot,
    KnuthCompleter,
    RunCompletion,
    ToolCompletion,
)
from knuth_cli.config import AgentConfig, default_config_path, load_config
from knuth_cli.input import InputResult, PromptToolkitInput, StreamInput
from knuth_cli.input_history import PromptHistory, resolve_project_key
from knuth_cli.prompts import build_cli_system_sections
from knuth_cli.runtime import build_runtime
from knuth_runtime import RunResult
from knuth_runtime.context import RunContext


def _write_yaml(path: Path, values: dict[str, object]) -> None:
    lines = []
    for key, value in values.items():
        if isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines), encoding="utf-8")


class _StreamingFakeRuntime:
    """Fake runtime that emits a content stream for session listeners."""

    def start(self, prompt, *, listeners=()):
        return _FakeRunSession(prompt, "run-1", listeners)

    def continue_run(self, run_id, prompt, *, listeners=()):
        return _FakeRunSession(prompt, run_id, listeners)

    def resume(self, run_id, *, listeners=()):
        return _FakeRunSession("resumed", run_id, listeners)


class _FailingFakeRuntime:
    def start(self, prompt, *, listeners=()):
        return _FailingRunSession()

    def continue_run(self, run_id, prompt, *, listeners=()):
        return _FailingRunSession()

    async def pending_approvals(self, run_id=None):
        return []


class _SkillFakeRuntime(_StreamingFakeRuntime):
    async def skills(self):
        return [
            SkillInfo(
                metadata=SkillMetadata(
                    name="example-skill",
                    description="Use when an example skill is needed.",
                ),
                source=SkillSource.PROJECT,
                file_path="/tmp/example-skill/SKILL.md",
            )
        ]


class _BrokenSkillRuntime(_StreamingFakeRuntime):
    async def skills(self):
        raise RuntimeError("catalog down")


class _UsageFakeRuntime(_StreamingFakeRuntime):
    async def events(self, run_id: str):
        self.requested_run_id = run_id
        return [
            type(
                "Event",
                (),
                {
                    "type": "model.completed",
                    "usage": UsageInfo(
                        input_tokens=10,
                        output_tokens=5,
                        total_tokens=15,
                        cost_usd=0.001,
                    ),
                },
            )(),
            type(
                "Event",
                (),
                {
                    "type": "model.completed",
                    "usage": UsageInfo(
                        input_tokens=7,
                        output_tokens=3,
                        total_tokens=10,
                        cost_usd=0.002,
                    ),
                },
            )(),
        ]


class _MissingUsageRunFakeRuntime(_StreamingFakeRuntime):
    async def events(self, run_id: str):
        self.requested_run_id = run_id
        return []


class _FakeRunSession:
    def __init__(self, prompt: str, run_id: str, listeners) -> None:
        self._prompt = prompt
        self._run_id = run_id
        self._listeners = tuple(listeners)

    async def __aenter__(self):
        answer = f"real-ish: {self._prompt}"
        event = emit_transient_runtime_event(
            self._run_id,
            ModelContentDeltaDraft(delta=answer),
            event_id="evt-1",
            created_at="2026-06-05T00:00:00Z",
        )
        for listener in self._listeners:
            if listener.interest.matches(event):
                await listener.handle_event(event)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def result(self) -> RunResult:
        return RunResult(
            answer=f"real-ish: {self._prompt}",
            run_id=self._run_id,
            status=RunStatus.SUCCEEDED,
        )


class _FailingRunSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def result(self) -> RunResult:
        raise RuntimeError("boom")


class _FakePromptInput:
    def __init__(
        self,
        *,
        prompts: list[InputResult] | None = None,
        approvals: list[InputResult] | None = None,
        records_history: bool = False,
    ) -> None:
        self.prompts = list(prompts or [])
        self.approvals = list(approvals or [])
        self.records_history = records_history

    async def read_prompt(self, prompt: str) -> InputResult:
        if self.prompts:
            return self.prompts.pop(0)
        return InputResult.eof()

    async def read_approval(self, prompt: str) -> InputResult:
        if self.approvals:
            return self.approvals.pop(0)
        return InputResult.eof()


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

    def test_load_config_allows_chatgpt_model_without_api_key(self) -> None:
        config = anyio.run(
            load_config,
            Path("does-not-exist.yaml"),
            {
                "KNUTH_MODEL": "chatgpt/gpt-5.3-codex",
                "KNUTH_CHATGPT_TOKEN_DIR": "/tmp/knuth-chatgpt",
            },
        )

        self.assertEqual(config.model, "chatgpt/gpt-5.3-codex")
        self.assertEqual(config.auth_mode, "chatgpt")
        self.assertEqual(config.chatgpt_token_dir, "/tmp/knuth-chatgpt")
        self.assertIsNone(config.api_key)
        self.assertIsNone(config.base_url)

    def test_load_config_rejects_unknown_auth_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "KNUTH_AUTH_MODE"):
            anyio.run(
                load_config,
                Path("does-not-exist.yaml"),
                {
                    "KNUTH_MODEL": "chatgpt/gpt-5.3-codex",
                    "KNUTH_AUTH_MODE": "chatgppt",
                },
            )

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
            ), patch("pathlib.Path.cwd", return_value=Path(temp_dir, "empty-cwd")):
                config = anyio.run(load_config, None, {})

            self.assertEqual(config.api_key, "default-key")
            self.assertEqual(config.base_url, "https://default.test/v1")
            self.assertEqual(config.model, "default-model")
            self.assertIsNone(config.system_prompt)

    def test_load_config_reads_repo_dotenv_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            cwd.joinpath(".env").write_text(
                "\n".join(
                    [
                        "KNUTH_API_KEY=dotenv-key",
                        "KNUTH_BASE_URL=https://dotenv.test/v1",
                        'KNUTH_MODEL="dotenv-model"',
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch("knuth_cli.config.default_config_path", return_value=cwd / "none.yaml"),
                patch("pathlib.Path.cwd", return_value=cwd),
            ):
                config = anyio.run(load_config, None, {})

            self.assertEqual(config.api_key, "dotenv-key")
            self.assertEqual(config.base_url, "https://dotenv.test/v1")
            self.assertEqual(config.model, "dotenv-model")

    def test_environment_values_override_repo_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            cwd.joinpath(".env").write_text(
                "\n".join(
                    [
                        "KNUTH_API_KEY=dotenv-key",
                        "KNUTH_BASE_URL=https://dotenv.test/v1",
                        "KNUTH_MODEL=dotenv-model",
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch("knuth_cli.config.default_config_path", return_value=cwd / "none.yaml"),
                patch("pathlib.Path.cwd", return_value=cwd),
            ):
                config = anyio.run(
                    load_config,
                    None,
                    {
                        "KNUTH_API_KEY": "env-key",
                        "KNUTH_BASE_URL": "https://env.test/v1",
                        "KNUTH_MODEL": "env-model",
                    },
                )

            self.assertEqual(config.api_key, "env-key")
            self.assertEqual(config.base_url, "https://env.test/v1")
            self.assertEqual(config.model, "env-model")

    def test_load_config_enables_skills_with_project_and_user_roots_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            _write_yaml(
                config_path,
                {
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                },
            )

            config = anyio.run(load_config, config_path, {})

        self.assertTrue(config.skill_hot_reload)
        self.assertEqual(config.skill_hot_reload_debounce_ms, 1000)
        self.assertEqual(
            [(root.source, Path(root.path)) for root in config.skill_roots],
            [
                (SkillSource.PROJECT, Path.cwd() / ".knuth" / "skills"),
                (SkillSource.USER, Path.home() / ".agents" / "skills"),
            ],
        )

    def test_skill_environment_overrides_skill_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            host_one = Path(temp_dir, "one")
            host_two = Path(temp_dir, "two")
            _write_yaml(
                config_path,
                {
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                },
            )

            config = anyio.run(
                load_config,
                config_path,
                {
                    "KNUTH_SKILL_ROOTS": os.pathsep.join(
                        [str(host_one), str(host_two)]
                    ),
                    "KNUTH_SKILL_HOT_RELOAD": "0",
                    "KNUTH_SKILL_HOT_RELOAD_DEBOUNCE_MS": "250",
                },
            )

        self.assertFalse(config.skill_hot_reload)
        self.assertEqual(config.skill_hot_reload_debounce_ms, 250)
        self.assertEqual(
            [(root.source, Path(root.path)) for root in config.skill_roots],
            [
                (SkillSource.HOST, host_one),
                (SkillSource.HOST, host_two),
            ],
        )

    def test_skill_hot_reload_debounce_zero_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            _write_yaml(
                config_path,
                {
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                    "skill_hot_reload_debounce_ms": 0,
                },
            )

            config = anyio.run(load_config, config_path, {})

        self.assertEqual(config.skill_hot_reload_debounce_ms, 0)


class CliRuntimeFactoryTests(unittest.TestCase):
    def test_cli_prompt_sections_keep_base_role_before_user_prompt(self) -> None:
        async def scenario():
            providers = build_cli_system_sections("USER PROMPT")
            ctx = RunContext(run_id="run-1")
            result = []
            for provider in providers:
                result.extend(await provider.sections(ctx))
            return result

        sections = anyio.run(scenario)

        self.assertEqual(sections[0].source, SystemSectionSource.BASE)
        self.assertIn("AI shell", sections[0].text)
        self.assertEqual(sections[1].source, SystemSectionSource.USER)
        self.assertEqual(sections[1].text, "USER PROMPT")

    def test_cli_prompt_sections_include_workspace_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "AGENTS.md").write_text(
                "Always answer with repo context.",
                encoding="utf-8",
            )

            async def scenario():
                providers = build_cli_system_sections(workspace=Path(temp_dir))
                ctx = RunContext(run_id="run-1")
                result = []
                for provider in providers:
                    result.extend(await provider.sections(ctx))
                return result

            sections = anyio.run(scenario)

        self.assertEqual(sections[-1].source, SystemSectionSource.USER)
        self.assertIn("# AGENTS.md", sections[-1].text)
        self.assertIn("Always answer with repo context.", sections[-1].text)

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
        self.assertIn("Knuth Shell", first_turn_messages[0].content or "")
        self.assertIn("USER PROMPT", first_turn_messages[0].content or "")

    def test_build_runtime_maps_chatgpt_token_dir_for_litellm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            db_path = Path(temp_dir, "knuth.db")
            token_dir = Path(temp_dir, "chatgpt")
            _write_yaml(
                config_path,
                {
                    "auth_mode": "chatgpt",
                    "model": "chatgpt/gpt-5.3-codex",
                    "chatgpt_token_dir": str(token_dir),
                },
            )

            async def make_runtime():
                return await build_runtime(config_path=config_path, db_path=db_path)

            with patch.dict(os.environ, {}, clear=True):
                anyio.run(make_runtime)
                self.assertEqual(os.environ["CHATGPT_TOKEN_DIR"], str(token_dir))

    def test_build_runtime_registers_cli_local_tools_after_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            db_path = Path(temp_dir, "knuth.db")
            _write_yaml(
                config_path,
                {
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                },
            )

            async def scenario():
                runtime = await build_runtime(config_path=config_path, db_path=db_path)
                tools = await runtime.tools()
                return {item["function"]["name"]: item["function"] for item in tools}

            by_name = anyio.run(scenario)

        self.assertIn("edit_file", by_name)
        self.assertIn("glob", by_name)
        self.assertIn("grep", by_name)
        self.assertIn("skill", by_name)
        self.assertIn("offset and line limit", by_name["read_file"]["description"])
        self.assertIn("glob pattern", by_name["glob"]["description"])
        self.assertIn("ripgrep regex syntax", by_name["grep"]["description"])
        self.assertIn("structured", by_name["shell"]["description"])


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

    def test_interactive_prompt_ctrl_c_stays_in_repl(self) -> None:
        output = io.StringIO()
        prompt_input = _FakePromptInput(
            prompts=[InputResult.cancelled(), InputResult.text_input("/exit")]
        )

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch("knuth_cli.repl._make_prompt_input", return_value=prompt_input),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(prompt_input.prompts, [])
        self.assertIn("Knuth agent ready", output.getvalue())

    def test_interactive_prompt_cancellation_stays_in_repl(self) -> None:
        output = io.StringIO()
        prompt_input = _FakePromptInput(
            prompts=[InputResult.cancelled(), InputResult.text_input("/exit")]
        )

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch("knuth_cli.repl._make_prompt_input", return_value=prompt_input),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(prompt_input.prompts, [])
        self.assertIn("Knuth agent ready", output.getvalue())

    def test_interactive_resume_slash_command_resumes_current_run(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("hello\n/resume\n/exit\n")

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("real-ish: hello", output.getvalue())
        self.assertIn("real-ish: resumed", output.getvalue())
        self.assertIn("run run-1 · succeeded", output.getvalue())

    def test_interactive_resume_slash_command_accepts_run_id(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("/resume run-2\n/exit\n")

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("real-ish: resumed", output.getvalue())
        self.assertIn("run run-2 · succeeded", output.getvalue())

    def test_unknown_leading_slash_is_sent_as_prompt(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("/not-a-command explain this\n/exit\n")

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("real-ish: /not-a-command explain this", output.getvalue())
        self.assertNotIn("Unknown command", output.getvalue())

    def test_skill_slash_command_starts_model_turn_and_records_raw_history(self) -> None:
        history = _RecordingHistory()
        prompt_input = _FakePromptInput(
            prompts=[
                InputResult.text_input("/skill:example-skill with care"),
                InputResult.text_input("/exit"),
            ],
            records_history=True,
        )
        output = io.StringIO()

        async def runtime_factory() -> _SkillFakeRuntime:
            return _SkillFakeRuntime()

        with (
            patch("knuth_cli.repl._make_prompt_history", return_value=history),
            patch("knuth_cli.repl._make_prompt_input", return_value=prompt_input),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(history.appended, ["/skill:example-skill with care"])
        self.assertIn(
            "Use the example-skill skill for this request before answering.",
            output.getvalue(),
        )
        self.assertIn("Skill command arguments:", output.getvalue())
        self.assertIn("with care", output.getvalue())

    def test_skill_builtin_command_accepts_skill_name_and_raw_args(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("/skill example-skill with care\n/exit\n")

        async def runtime_factory() -> _SkillFakeRuntime:
            return _SkillFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("Use the example-skill skill", output.getvalue())
        self.assertIn("with care", output.getvalue())

    def test_skill_slash_command_catalog_error_is_not_sent_as_prompt(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("/skill:example-skill with care\n/exit\n")

        async def runtime_factory() -> _BrokenSkillRuntime:
            return _BrokenSkillRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        text = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Could not load commands: RuntimeError: catalog down", text)
        self.assertNotIn("real-ish: /skill:example-skill with care", text)

    def test_help_uses_builtin_catalog_when_skill_catalog_fails(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("/help\n/exit\n")

        async def runtime_factory() -> _BrokenSkillRuntime:
            return _BrokenSkillRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        text = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("/usage", text)
        self.assertNotIn("catalog down", text)

    def test_usage_slash_command_reports_current_run_token_usage(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("hello\n/usage\n/exit\n")

        async def runtime_factory() -> _UsageFakeRuntime:
            return _UsageFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        text = output.getvalue()
        self.assertIn("run-1 usage", text)
        self.assertIn("model calls: 2", text)
        self.assertIn("input tokens: 17", text)
        self.assertIn("output tokens: 8", text)
        self.assertIn("total tokens: 25", text)
        self.assertIn("cost: $0.003000", text)

    def test_usage_slash_command_reports_missing_run(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("/usage missing-run\n/exit\n")

        async def runtime_factory() -> _MissingUsageRunFakeRuntime:
            return _MissingUsageRunFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("Run not found: missing-run", output.getvalue())

    def test_help_slash_command_uses_catalog_with_usage_and_skills(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("/help\n/exit\n")

        async def runtime_factory() -> _SkillFakeRuntime:
            return _SkillFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        text = output.getvalue()
        self.assertIn("/usage", text)
        self.assertIn("/skill:example-skill", text)

    def test_interactive_run_reports_turn_errors_without_crashing_repl(self) -> None:
        output = io.StringIO()
        input_stream = io.StringIO("hello\n/exit\n")

        async def runtime_factory() -> _FailingFakeRuntime:
            return _FailingFakeRuntime()

        with (
            patch("sys.stdin", input_stream),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("Run failed: RuntimeError: boom", output.getvalue())

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

            def resume(self, run_id: str, *, listeners=()):
                return _FakeRunSession("resumed", run_id, listeners)

            async def refold(self):
                from knuth_runtime import RefoldStats

                return RefoldStats(runs=2, events=17)

            async def recover_crashed_runs(self, run_id=None):
                from knuth_runtime import CrashRecoveryReport

                return [CrashRecoveryReport(run_id="run-9", failed=1, unknown=1)]

        async def runtime_factory() -> FakeRuntime:
            return FakeRuntime()

        for argv, expected in [
            (["status", "run-1"], "succeeded"),
            (["events", "run-1"], "run.succeeded"),
            (["tools", "list"], "read_file"),
            (["approve", "appr-1"], "appr-1"),
            (["deny", "appr-1"], "appr-1"),
            (["resume", "run-1"], "resumed"),
            (["admin", "refold"], "refolded 2 runs from 17 events"),
            (["recover"], "run-9\tpaused\tfailed=1\tunknown=1"),
        ]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(argv, runtime_factory=runtime_factory)
            self.assertEqual(exit_code, 0)
            self.assertIn(expected, output.getvalue())

    def test_global_flags_reach_the_runtime_factory(self) -> None:
        captured = {}

        class FakeRuntime:
            async def status(self, run_id: str) -> RunStatus:
                return RunStatus.SUCCEEDED

        async def runtime_factory(*, enable_plugins=False, debug=False):
            captured.update(enable_plugins=enable_plugins, debug=debug)
            return FakeRuntime()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(
                ["--debug", "--enable-plugins", "status", "run-1"],
                runtime_factory=runtime_factory,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(captured, {"enable_plugins": True, "debug": True})


class _RaisingStream:
    def __init__(self, *items: object) -> None:
        self._items = list(items)

    def readline(self) -> str:
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return str(item)


class StreamInputTests(unittest.TestCase):
    def _console(self) -> Console:
        return Console(file=io.StringIO(), force_terminal=False, width=100)

    def test_read_prompt_returns_text(self) -> None:
        async def scenario():
            prompt_input = StreamInput(
                self._console(), input_stream=io.StringIO("hello\n")
            )
            return await prompt_input.read_prompt("knuth ❯ ")

        result = anyio.run(scenario)

        self.assertEqual(result, InputResult.text_input("hello"))

    def test_read_prompt_returns_eof(self) -> None:
        async def scenario():
            prompt_input = StreamInput(self._console(), input_stream=io.StringIO(""))
            return await prompt_input.read_prompt("knuth ❯ ")

        result = anyio.run(scenario)

        self.assertEqual(result, InputResult.eof())

    def test_keyboard_interrupt_returns_cancelled(self) -> None:
        async def scenario():
            prompt_input = StreamInput(
                self._console(), input_stream=_RaisingStream(KeyboardInterrupt())
            )
            return await prompt_input.read_prompt("knuth ❯ ")

        result = anyio.run(scenario)

        self.assertEqual(result, InputResult.cancelled())

    def test_decode_error_retries_next_line(self) -> None:
        async def scenario():
            prompt_input = StreamInput(
                self._console(),
                input_stream=_RaisingStream(
                    UnicodeDecodeError(
                        "utf-8", b"\xef", 0, 1, "invalid continuation byte"
                    ),
                    "ok\n",
                ),
            )
            return await prompt_input.read_prompt("knuth ❯ ")

        result = anyio.run(scenario)

        self.assertEqual(result, InputResult.text_input("ok"))


class _FakePromptToolkitSession:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def prompt_async(self, prompt: str, **kwargs: object) -> str:
        self.calls.append((prompt, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return str(result)


class PromptToolkitInputTests(unittest.TestCase):
    def _history(self, path: Path) -> PromptHistory:
        return PromptHistory(path=path)

    def test_prompt_toolkit_input_does_not_write_history_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = _FakePromptToolkitSession("hello\tworld")
            history = self._history(Path(temp_dir, "history.jsonl"))
            prompt_input = PromptToolkitInput(
                history=history,
                prompt_session=session,
                approval_session=_FakePromptToolkitSession("y"),
            )

            result = anyio.run(prompt_input.read_prompt, "knuth ❯ ")

            self.assertEqual(result, InputResult.text_input("hello    world"))
            self.assertEqual(session.calls[0][1], {})
            history.store_string("toolkit attempted write")
            self.assertFalse(Path(temp_dir, "history.jsonl").exists())

    def test_approval_uses_separate_session_without_history_append(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            approval_session = _FakePromptToolkitSession("y")
            prompt_input = PromptToolkitInput(
                history=self._history(Path(temp_dir, "history.jsonl")),
                prompt_session=_FakePromptToolkitSession("ignored"),
                approval_session=approval_session,
            )

            result = anyio.run(prompt_input.read_approval, "approve? ")

            self.assertEqual(result, InputResult.text_input("y"))
            self.assertEqual(approval_session.calls[0][1], {})

    def test_ctrl_c_and_eof_are_typed_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_input = PromptToolkitInput(
                history=self._history(Path(temp_dir, "history.jsonl")),
                prompt_session=_FakePromptToolkitSession(KeyboardInterrupt()),
                approval_session=_FakePromptToolkitSession(EOFError()),
            )

            self.assertEqual(
                anyio.run(prompt_input.read_prompt, "knuth ❯ "),
                InputResult.cancelled(),
            )
            self.assertEqual(
                anyio.run(prompt_input.read_approval, "approve? "),
                InputResult.eof(),
            )


class PromptHistoryTests(unittest.TestCase):
    def test_project_key_uses_git_root_for_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath(".git").mkdir()
            subdir = root / "a" / "b"
            subdir.mkdir(parents=True)

            self.assertEqual(resolve_project_key(subdir), str(root.resolve()))

    def test_project_key_falls_back_to_cwd_realpath_outside_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)

            self.assertEqual(resolve_project_key(cwd), str(cwd.resolve()))

    def test_append_prompt_writes_project_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath(".git").mkdir()
            history_path = root / "history.jsonl"
            history = PromptHistory(
                path=history_path,
                cwd=root / "subdir",
                session_id="session-1",
            )

            history.append_prompt("explain this")

            [line] = history_path.read_text(encoding="utf-8").splitlines()
            record = json.loads(line)
            self.assertEqual(record["text"], "explain this")
            self.assertEqual(record["project_key"], str(root.resolve()))
            self.assertEqual(record["cwd"], str((root / "subdir").resolve()))
            self.assertEqual(record["session_id"], "session-1")
            self.assertEqual(record["kind"], "prompt")

    def test_consecutive_duplicates_are_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            history = PromptHistory(path=Path(temp_dir, "history.jsonl"))

            history.append_prompt("same")
            history.append_prompt("same")

            lines = history.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)

    def test_non_consecutive_duplicates_are_kept_but_navigation_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            history = PromptHistory(path=Path(temp_dir, "history.jsonl"))

            history.append_prompt("one")
            history.append_prompt("two")
            history.append_prompt("one")

            lines = history.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(list(history.load_history_strings()), ["one", "two"])

    def test_new_session_changes_metadata_without_hiding_project_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            history = PromptHistory(
                path=Path(temp_dir, "history.jsonl"),
                session_id="session-1",
            )

            history.append_prompt("before")
            history.start_new_session()
            history.append_prompt("after")

            records = [
                json.loads(line)
                for line in history.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[0]["session_id"], "session-1")
            self.assertNotEqual(records[1]["session_id"], "session-1")
            self.assertEqual(list(history.load_history_strings()), ["after", "before"])

    def test_read_failure_degrades_to_memory_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blocked = Path(temp_dir, "blocked")
            blocked.mkdir()
            history = PromptHistory(path=blocked)

            history.append_prompt("memory only")

            self.assertEqual(list(history.load_history_strings()), ["memory only"])


class _RecordingHistory:
    def __init__(self) -> None:
        self.appended: list[str] = []
        self.session_count = 0

    def append_prompt(self, text: str) -> None:
        self.appended.append(text)

    def start_new_session(self) -> None:
        self.session_count += 1


class ReplHistoryWriteTests(unittest.TestCase):
    def test_prompt_history_is_written_before_failing_turn(self) -> None:
        history = _RecordingHistory()
        prompt_input = _FakePromptInput(
            prompts=[InputResult.text_input("hello"), InputResult.text_input("/exit")],
            records_history=True,
        )
        output = io.StringIO()

        async def runtime_factory() -> _FailingFakeRuntime:
            return _FailingFakeRuntime()

        with (
            patch("knuth_cli.repl._make_prompt_history", return_value=history),
            patch("knuth_cli.repl._make_prompt_input", return_value=prompt_input),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(history.appended, ["hello"])
        self.assertIn("Run failed: RuntimeError: boom", output.getvalue())

    def test_slash_exit_and_approval_inputs_do_not_write_prompt_history(self) -> None:
        history = _RecordingHistory()
        prompt_input = _FakePromptInput(
            prompts=[InputResult.text_input("/help"), InputResult.text_input("/exit")],
            records_history=True,
        )
        output = io.StringIO()

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch("knuth_cli.repl._make_prompt_history", return_value=history),
            patch("knuth_cli.repl._make_prompt_input", return_value=prompt_input),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(history.appended, [])

    def test_skill_builtin_command_writes_raw_prompt_history(self) -> None:
        history = _RecordingHistory()
        prompt_input = _FakePromptInput(
            prompts=[
                InputResult.text_input("/skill example-skill with care"),
                InputResult.text_input("/exit"),
            ],
            records_history=True,
        )
        output = io.StringIO()

        async def runtime_factory() -> _SkillFakeRuntime:
            return _SkillFakeRuntime()

        with (
            patch("knuth_cli.repl._make_prompt_history", return_value=history),
            patch("knuth_cli.repl._make_prompt_input", return_value=prompt_input),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(history.appended, ["/skill example-skill with care"])


class CompletionTests(unittest.TestCase):
    def _texts(self, text: str, manager: CompletionManager | None = None) -> list[str]:
        manager = manager or CompletionManager()
        completer = KnuthCompleter(manager)
        return [
            completion.text
            for completion in completer.get_completions(Document(text), None)
        ]

    def test_completes_slash_commands(self) -> None:
        self.assertIn("/resume", self._texts("/res"))
        self.assertIn("/usage", self._texts("/us"))

    def test_completes_run_ids_from_snapshot(self) -> None:
        manager = CompletionManager()
        manager.snapshot = CompletionSnapshot(
            runs=(
                RunCompletion(id="run-1", status="paused"),
                RunCompletion(id="run-2", status="succeeded"),
            )
        )

        self.assertEqual(self._texts("/resume run-", manager), ["run-1", "run-2"])
        self.assertEqual(self._texts("/status run-2", manager), ["run-2"])

    def test_completes_tool_names_from_snapshot(self) -> None:
        manager = CompletionManager()
        manager.snapshot = CompletionSnapshot(
            tools=(
                ToolCompletion(name="read_file", description="Read"),
                ToolCompletion(name="write_file", description="Write"),
            )
        )

        self.assertEqual(self._texts("/tools read", manager), ["read_file"])

    def test_completes_skill_commands_from_runtime_snapshot(self) -> None:
        manager = CompletionManager()

        anyio.run(manager.refresh, _SkillFakeRuntime())

        self.assertIn("/skill:example-skill", self._texts("/skill:exa", manager))
        self.assertIn("/example-skill", self._texts("/exa", manager))

    def test_completion_path_only_reads_snapshot(self) -> None:
        class ExplodingRuntime:
            async def runs(self, limit=20):
                raise AssertionError("runtime should not be called")

            async def tools(self):
                raise AssertionError("runtime should not be called")

        manager = CompletionManager()
        manager.runtime = ExplodingRuntime()

        self.assertEqual(self._texts("/too", manager), ["/tools"])


if __name__ == "__main__":
    unittest.main()
