import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth.core.types import RunStatus
from knuth_llmd import (
    InferenceConfig,
    InferenceEvent,
    InferenceEventType,
    InferenceResult,
)
from knuth_runtime import (
    AgentLoop,
    MemoryEventStore,
    MemoryRunStore,
    build_default_runtime,
    build_memory_runtime,
)
from knuth_runtime.approval import MemoryApprovalService
from knuth_runtime.artifact_store import MemoryArtifactStore
from knuth_runtime.context import reconstruct_messages_from_events
from knuth_runtime.hooks import HookAction, HookContext, HookManager, HookRegistration, HookResult
from knuth_runtime.policy import PolicyEngine
from knuth_toold import ToolBroker, create_default_registry


class ScriptedClient:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        messages: list[InferenceMessage],
        config: InferenceConfig,
        tools=(),
        runtime=None,
    ) -> InferenceResult:
        self.calls += 1
        if self.calls == 1:
            return InferenceResult(
                message=InferenceMessage(
                    role=InferenceRole.ASSISTANT,
                    content="Reading file",
                    tool_calls=[
                        CoreToolCall(
                            name="read_file",
                            arguments={"path": "fact.txt"},
                        )
                    ],
                ),
            )
        tool_message = messages[-1]
        return InferenceResult(
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT,
                content=f"Final answer: {tool_message.content}",
            )
        )


class AgentLoopTests(unittest.TestCase):
    def test_agent_loop_executes_tool_calls_then_returns_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "fact.txt").write_text("Knuth works", encoding="utf-8")
            loop = AgentLoop(
                inference_client=ScriptedClient(),
                inference_config=InferenceConfig(model="scripted-model"),
                tool_executor=create_default_registry(Path(workspace)),
            )

            turn = anyio.run(loop.run_turn, "read fact.txt")

            self.assertEqual(turn.answer, "Final answer: Knuth works")
            self.assertEqual([call.name for call in turn.tool_calls], ["read_file"])

    def test_build_default_runtime_does_not_pass_workspace_to_toold(self) -> None:
        with (
            patch("knuth_runtime.agent.load_llm_config") as load_config,
            patch("knuth_runtime.agent.LiteLLMInferenceClient") as client_class,
            patch("knuth_runtime.agent.create_default_registry") as create_registry,
        ):
            load_config.return_value = type(
                "Config",
                (),
                {
                    "model": "test-model",
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "timeout": 60.0,
                },
            )()
            client_class.return_value = ScriptedClient()
            create_registry.return_value = create_default_registry(Path.cwd())

            runtime = anyio.run(build_default_runtime)

            self.assertIsNotNone(runtime)
            create_registry.assert_called_once_with()


class ScriptedInferenceClient:
    def __init__(self, messages: list[InferenceMessage]) -> None:
        self.messages = messages
        self.calls = 0

    async def complete(self, messages, config, runtime=None):
        raise NotImplementedError

    async def stream(self, messages, tools, config, runtime=None):
        message = self.messages[min(self.calls, len(self.messages) - 1)]
        self.calls += 1
        yield InferenceEvent(
            type=InferenceEventType.GENERATION_END,
            generation_id=f"gen-{self.calls}",
            seq=1,
            run_id=config.run_id,
            payload={"message": message.model_dump()},
        )


class EventDrivenRuntimeTests(unittest.TestCase):
    def build_runtime(self, workspace: str, messages: list[InferenceMessage]):
        run_store = MemoryRunStore()
        event_store = MemoryEventStore()
        approvals = MemoryApprovalService()
        registry = create_default_registry(Path(workspace))
        broker = ToolBroker(registry, PolicyEngine(approvals))
        return build_memory_runtime(
            inference_client=ScriptedInferenceClient(messages),
            inference_config=InferenceConfig(model="scripted-model"),
            run_store=run_store,
            event_store=event_store,
            approvals=approvals,
            tool_broker=broker,
        )

    def test_event_driven_runtime_executes_tool_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "fact.txt").write_text("Knuth works", encoding="utf-8")
            runtime = self.build_runtime(
                workspace,
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content="",
                        tool_calls=[
                            CoreToolCall(
                                id="call-1",
                                name="read_file",
                                arguments={"path": "fact.txt"},
                            )
                        ],
                    ),
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content="Final answer: Knuth works",
                    ),
                ],
            )

            turn = anyio.run(runtime.run_once, "read fact.txt")
            events = anyio.run(runtime.events, turn.run_id)

            self.assertEqual(turn.status, RunStatus.SUCCEEDED)
            self.assertEqual(turn.answer, "Final answer: Knuth works")
            self.assertIn(("tool", "completed"), [(e.namespace, e.name) for e in events])
            self.assertIn(("run", "succeeded"), [(e.namespace, e.name) for e in events])
            self.assertNotIn(
                ("model", "content_delta"), [(e.namespace, e.name) for e in events]
            )
            reconstructed = reconstruct_messages_from_events(events)
            self.assertEqual(reconstructed[-1].content, "Final answer: Knuth works")

    def test_ask_user_tool_sets_waiting_user(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            runtime = self.build_runtime(
                workspace,
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        tool_calls=[
                            CoreToolCall(
                                id="call-ask",
                                name="knuth.ask_user",
                                arguments={"question": "Which file?"},
                            )
                        ],
                    )
                ],
            )

            turn = anyio.run(runtime.run_once, "read something")

            self.assertEqual(turn.status, RunStatus.WAITING_USER)
            self.assertEqual(turn.answer, "Which file?")

    def test_approval_resume_executes_pending_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            runtime = self.build_runtime(
                workspace,
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        tool_calls=[
                            CoreToolCall(
                                id="call-write",
                                name="write_file",
                                arguments={"path": "x.txt", "content": "hello"},
                            )
                        ],
                    ),
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content="Done",
                    ),
                ],
            )

            first = anyio.run(runtime.run_once, "write x")
            pending = anyio.run(runtime.pending_approvals, first.run_id)
            anyio.run(runtime.approve, pending[0].id)
            resumed = anyio.run(runtime.resume, first.run_id)

            self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
            self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
            self.assertEqual(Path(workspace, "x.txt").read_text(encoding="utf-8"), "hello")

    def test_hook_manager_can_pause_blocking_flow(self) -> None:
        hooks = HookManager()

        async def pause(ctx: HookContext) -> HookResult:
            return HookResult(action=HookAction.PAUSE, reason="test")

        hooks.register(
            HookRegistration(
                namespace="run",
                name="before_step",
                handler_id="pause",
                blocking=True,
            ),
            pause,
        )

        result = anyio.run(
            hooks.dispatch_blocking,
            HookContext(run_id="run-1", namespace="run", name="before_step"),
        )

        self.assertEqual(result.action, HookAction.PAUSE)

    def test_memory_artifact_store_round_trips_text(self) -> None:
        store = MemoryArtifactStore()

        artifact = anyio.run(
            store.put_text,
            "run-1",
            "note",
            "summary",
            "hello artifact",
        )
        content = anyio.run(store.get_text, artifact.id)

        self.assertEqual(artifact.kind, "note")
        self.assertEqual(content, "hello artifact")


if __name__ == "__main__":
    unittest.main()
