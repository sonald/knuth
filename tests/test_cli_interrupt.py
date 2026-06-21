"""Phase 4 acceptance: CLI interrupt driver, reentry, and approval Ctrl-C."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import anyio
from rich.console import Console

from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth.core.types import RunStatus
from knuth_cli.input import InputResult
from knuth_llmd import InferenceConfig
from knuth_runtime import MemoryRunLedger, build_memory_runtime
from knuth_runtime.policy import PolicyEngine
from knuth_toold import ToolBroker, create_default_registry
import knuth_cli.repl as repl


class _FakePromptInput:
    def __init__(self, *, approvals: list[InputResult] | None = None) -> None:
        self.approvals = list(approvals or [])

    async def read_prompt(self, prompt: str) -> InputResult:
        return InputResult.eof()

    async def read_approval(self, prompt: str) -> InputResult:
        if self.approvals:
            return self.approvals.pop(0)
        return InputResult.eof()


def _runtime(messages):
    _scripted = messages

    class _Client:
        model = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, tools, config, runtime=None):
            from knuth.core.events import InferenceGenerationCompleted

            message = _scripted[min(self.calls, len(_scripted) - 1)]
            self.calls += 1
            yield InferenceGenerationCompleted(
                generation_id=f"g{self.calls}",
                seq=1,
                run_id=config.run_id,
                message=message,
            )

    registry = create_default_registry()
    broker = ToolBroker(registry, PolicyEngine())
    return build_memory_runtime(
        inference_client=_Client(),
        inference_config=InferenceConfig(),
        ledger=MemoryRunLedger(),
        tool_broker=broker,
    )


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=False, width=100), buffer


class ApprovalCtrlCTests(unittest.TestCase):
    def test_approval_ctrl_c_leaves_waiting_approval(self) -> None:
        async def scenario():
            runtime = _runtime(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        tool_calls=[
                            CoreToolCall(
                                tool_call_id="c1",
                                name="write_file",
                                arguments={"path": "x.txt", "content": "hi"},
                            )
                        ],
                    ),
                    InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
                ]
            )
            console, _ = _console()
            prompt_input = _FakePromptInput(approvals=[InputResult.cancelled()])
            # Run a turn that lands on WAITING_APPROVAL, then hit Ctrl-C at the
            # approval prompt.
            run_id, result = await repl._run_turn(
                runtime, console, "write x", None, set(), prompt_input
            )
            status = await runtime.status(run_id)
            events = await runtime.events(run_id)
            return status, events

        status, events = anyio.run(scenario)
        # Ctrl-C only exited the local approval UI; the run is still actionable.
        self.assertEqual(status, RunStatus.WAITING_APPROVAL)
        self.assertNotIn("run.interrupted", [e.type for e in events])

    def test_reentry_reshows_pending_approval(self) -> None:
        async def scenario():
            runtime = _runtime(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        tool_calls=[
                            CoreToolCall(
                                tool_call_id="c1",
                                name="write_file",
                                arguments={"path": "x.txt", "content": "hi"},
                            )
                        ],
                    ),
                ]
            )
            console, buffer = _console()
            # Reach WAITING_APPROVAL.
            await repl._run_turn(
                runtime,
                console,
                "write x",
                None,
                set(),
                _FakePromptInput(approvals=[InputResult.cancelled()]),
            )
            # Re-enter: reentry must re-show the pending approval. We feed a
            # cancellation again so the approval UI exits without resolving.
            adopted = await repl._reenter_actionable(
                runtime,
                console,
                set(),
                _FakePromptInput(approvals=[InputResult.cancelled()]),
            )
            return adopted, buffer.getvalue()

        adopted, output = anyio.run(scenario)
        self.assertIsNotNone(adopted)
        self.assertIn("waiting for approval", output)


class ReentryStatusTests(unittest.TestCase):
    def test_running_run_without_live_session_is_not_auto_recovered(self) -> None:
        from knuth.core.runtime_events import (
            ContextSnapshot,
            StepStartedDraft,
            UserMessageDraft,
        )

        async def scenario():
            ledger = MemoryRunLedger()
            run = await ledger.create_run("q")
            await ledger.apply(run.id, UserMessageDraft(content="q"))
            await ledger.apply(
                run.id,
                StepStartedDraft(
                    step_id="s1",
                    index=1,
                    snapshot=ContextSnapshot(
                        messages_hash="m",
                        tools_hash="t",
                        preamble_hash="p",
                        model_config_hash="c",
                        message_count=1,
                        tool_count=0,
                    ),
                ),
            )
            # The run is left RUNNING (a crashed/abandoned process). A fresh
            # interactive entry must not auto-recover it.
            runtime = build_memory_runtime(
                inference_client=_DummyClient(),
                inference_config=InferenceConfig(),
                ledger=ledger,
            )
            console, buffer = _console()
            adopted = await repl._reenter_actionable(
                runtime, console, set(), _FakePromptInput()
            )
            status = await runtime.status(run.id)
            return adopted, status, buffer.getvalue()

        adopted, status, output = anyio.run(scenario)
        self.assertIsNone(adopted)
        self.assertEqual(status, RunStatus.RUNNING)
        self.assertIn("no live session", output)

    def test_resume_slash_refuses_interrupted_run(self) -> None:
        class _Runtime:
            resume_called = False

            async def status(self, run_id: str) -> RunStatus:
                return RunStatus.INTERRUPTED

            def resume(self, run_id: str, *, listeners=()):
                self.resume_called = True
                raise AssertionError("interrupted runs must not resume")

        async def scenario():
            runtime = _Runtime()
            console, buffer = _console()
            adopted = await repl._handle_slash(
                runtime, console, "/resume run-1", None, set(), _FakePromptInput()
            )
            return adopted, runtime.resume_called, buffer.getvalue()

        adopted, resume_called, output = anyio.run(scenario)
        self.assertEqual(adopted, "run-1")
        self.assertFalse(resume_called)
        self.assertIn("was interrupted", output)


class _DummyClient:
    model = "dummy"

    async def stream(self, messages, tools, config, runtime=None):
        from knuth.core.events import InferenceGenerationCompleted

        yield InferenceGenerationCompleted(
            generation_id="g1",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(role=InferenceRole.ASSISTANT, content="x"),
        )


if __name__ == "__main__":
    unittest.main()
