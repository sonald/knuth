import asyncio
import contextlib
import io
import os
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
from knuth.core.messages import InferenceMessage, InferenceRole, SystemSectionSource
from knuth.core.skills import SkillSource
from knuth.core.types import RunStatus
from knuth_cli.cli import main
from knuth_cli.config import AgentConfig, default_config_path, load_config
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

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch(
                "knuth_cli.repl._read_line",
                side_effect=[KeyboardInterrupt, "/exit"],
            ) as read_line,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(read_line.call_count, 2)
        self.assertIn("Knuth agent ready", output.getvalue())

    def test_interactive_prompt_cancellation_stays_in_repl(self) -> None:
        output = io.StringIO()

        async def runtime_factory() -> _StreamingFakeRuntime:
            return _StreamingFakeRuntime()

        with (
            patch(
                "knuth_cli.repl._read_line",
                side_effect=[asyncio.CancelledError(), "/exit"],
            ) as read_line,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["run"], runtime_factory=runtime_factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(read_line.call_count, 2)
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


class _BlockingFakeStdin:
    """Stdin double whose readline blocks until a line is pushed."""

    def __init__(self) -> None:
        import queue

        self._lines: "queue.Queue[object]" = queue.Queue()

    def push(self, line: object) -> None:
        self._lines.put(line)

    def readline(self) -> str:
        line = self._lines.get()
        if isinstance(line, BaseException):
            raise line
        return str(line)


class StdinReaderTests(unittest.TestCase):
    """The single-reader discipline: an abandoned read must never leak its
    line into (or tear) the next prompt — the bug behind the CJK
    UnicodeDecodeError crash in the REPL."""

    def test_abandoned_request_drops_its_line(self) -> None:
        from knuth_cli.repl import _StdinReader

        fake = _BlockingFakeStdin()
        reader = _StdinReader()
        with patch("sys.stdin", fake):
            first = reader.submit()
            first.abandoned = True  # caller hit Ctrl-C and gave up
            second = reader.submit()
            fake.push("stale line\n")  # typed for the abandoned prompt
            fake.push("fresh line\n")
            self.assertTrue(second.done.wait(timeout=5))

        self.assertFalse(first.done.is_set())
        self.assertEqual(second.line, "fresh line")

    def test_preserved_abandoned_request_hands_line_to_next_read(self) -> None:
        from knuth_cli.repl import _StdinReader

        fake = _BlockingFakeStdin()
        reader = _StdinReader()
        with patch("sys.stdin", fake):
            first = reader.submit()
            first.abandoned = True
            first.preserve_late_line = True
            second = reader.submit()
            fake.push("next prompt line\n")
            self.assertTrue(second.done.wait(timeout=5))

        self.assertFalse(first.done.is_set())
        self.assertEqual(second.line, "next prompt line")

    def test_reads_are_served_strictly_in_order(self) -> None:
        from knuth_cli.repl import _StdinReader

        fake = _BlockingFakeStdin()
        reader = _StdinReader()
        with patch("sys.stdin", fake):
            first = reader.submit()
            second = reader.submit()
            fake.push("one\n")
            fake.push("two\n")
            self.assertTrue(first.done.wait(timeout=5))
            self.assertTrue(second.done.wait(timeout=5))

        self.assertEqual(first.line, "one")
        self.assertEqual(second.line, "two")

    def test_decode_error_is_surfaced_not_fatal(self) -> None:
        from knuth_cli.repl import _DECODE_ERROR, _StdinReader

        fake = _BlockingFakeStdin()
        reader = _StdinReader()
        with patch("sys.stdin", fake):
            request = reader.submit()
            fake.push(
                UnicodeDecodeError("utf-8", b"\xef", 0, 1, "invalid continuation byte")
            )
            self.assertTrue(request.done.wait(timeout=5))
            # The reader thread survives and serves the next request.
            recovered = reader.submit()
            fake.push("ok\n")
            self.assertTrue(recovered.done.wait(timeout=5))

        self.assertIs(request.line, _DECODE_ERROR)
        self.assertEqual(recovered.line, "ok")

    def test_eof_resolves_to_none(self) -> None:
        from knuth_cli.repl import _StdinReader

        fake = _BlockingFakeStdin()
        reader = _StdinReader()
        with patch("sys.stdin", fake):
            request = reader.submit()
            fake.push("")  # readline returning "" means EOF
            self.assertTrue(request.done.wait(timeout=5))

        self.assertIsNone(request.line)

    def test_torn_utf8_bytes_from_byte_wise_erase_do_not_poison_next_read(self) -> None:
        """Backspacing over CJK input without IUTF8 leaves partial bytes in
        the kernel line buffer. The torn line must surface as a decode error
        and be fully consumed, leaving the next read clean."""
        import queue as queue_module

        from knuth_cli.repl import _DECODE_ERROR, _StdinReader

        class _BlockingBytesStdin:
            encoding = "utf-8"

            def __init__(self) -> None:
                self._lines: "queue_module.Queue[bytes]" = queue_module.Queue()
                self.buffer = self

            def push(self, line: bytes) -> None:
                self._lines.put(line)

            def readline(self) -> bytes:
                return self._lines.get()

        fake = _BlockingBytesStdin()
        reader = _StdinReader()
        with patch("sys.stdin", fake):
            torn = reader.submit()
            # "进" (E8 BF 9B) backspaced once at the byte level, then "程".
            fake.push("x".encode() + b"\xe8\xbf" + "程\n".encode())
            self.assertTrue(torn.done.wait(timeout=5))
            clean = reader.submit()
            fake.push("显示进程\n".encode())
            self.assertTrue(clean.done.wait(timeout=5))

        self.assertIs(torn.line, _DECODE_ERROR)
        self.assertEqual(clean.line, "显示进程")


if __name__ == "__main__":
    unittest.main()
