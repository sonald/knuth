import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from knuth.core.events import (
    InferenceContentDelta,
    InferenceGenerationCompleted,
    RunCreatedDraft,
)
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig
from knuth_runtime import (
    MemoryEventStore,
    MemoryRunStore,
    SQLiteStore,
    build_default_runtime,
    build_memory_runtime,
)
from knuth_runtime.approval import MemoryApprovalService
from knuth_runtime.context import reconstruct_messages_from_events
from knuth_runtime.policy import PolicyEngine
from knuth_toold import ToolBroker, create_default_registry


class EventStoreTests(unittest.TestCase):
    def test_append_stores_strongly_typed_runtime_event(self) -> None:
        store = MemoryEventStore()

        event = anyio.run(
            store.append,
            "run-1",
            RunCreatedDraft(query="hello", metadata={"workspace_uri": "file:///tmp"}),
        )

        self.assertEqual(event.type, "run.created")
        self.assertEqual(event.run_id, "run-1")
        self.assertEqual(event.seq, 1)
        self.assertEqual(event.query, "hello")
        self.assertEqual(event.metadata["workspace_uri"], "file:///tmp")
        self.assertFalse(hasattr(event, "namespace"))
        self.assertFalse(hasattr(event, "name"))
        self.assertFalse(hasattr(event, "payload"))

    def test_sqlite_store_round_trips_typed_runtime_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir, "knuth.db"))

            appended = anyio.run(
                store.append,
                "run-1",
                RunCreatedDraft(query="hello", metadata={"workspace_uri": "file:///tmp"}),
            )
            listed = anyio.run(store.list_events, "run-1")

        self.assertEqual(appended.type, "run.created")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].query, "hello")
        self.assertEqual(listed[0].metadata["workspace_uri"], "file:///tmp")
        self.assertFalse(hasattr(listed[0], "namespace"))
        self.assertFalse(hasattr(listed[0], "name"))
        self.assertFalse(hasattr(listed[0], "payload"))

    def test_sqlite_store_rejects_legacy_event_schema(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir, "knuth.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table events (
                      id text primary key,
                      run_id text not null,
                      seq integer not null,
                      namespace text not null,
                      name text not null,
                      type text not null,
                      payload_json text not null,
                      durability text not null,
                      created_at text not null
                    )
                    """
                )

            with self.assertRaisesRegex(RuntimeError, "breaking event schema"):
                SQLiteStore(db_path)


class RuntimeFactoryTests(unittest.TestCase):
    def test_build_default_runtime_does_not_pass_workspace_to_toold(self) -> None:
        with (
            patch("knuth_runtime.agent.load_config") as load_config,
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
            client_class.return_value = object()
            create_registry.return_value = create_default_registry(Path.cwd())

            with tempfile.TemporaryDirectory() as temp_dir:
                runtime = anyio.run(
                    build_default_runtime, Path(temp_dir, "knuth.db")
                )

            self.assertIsNotNone(runtime)
            create_registry.assert_called_once_with()


class ScriptedInferenceClient:
    model = "scripted-model"

    def __init__(self, messages: list[InferenceMessage]) -> None:
        self.messages = messages
        self.calls = 0

    async def stream(self, messages, tools, config, runtime=None):
        message = self.messages[min(self.calls, len(self.messages) - 1)]
        self.calls += 1
        yield InferenceGenerationCompleted(
            generation_id=f"gen-{self.calls}",
            seq=1,
            run_id=config.run_id,
            message=message,
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
            inference_config=InferenceConfig(),
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
            self.assertIn("tool.completed", [e.type for e in events])
            self.assertIn("run.succeeded", [e.type for e in events])
            started = [
                e for e in events if e.type == "model.started"
            ]
            self.assertEqual(started[0].model, "scripted-model")
            self.assertNotIn(
                "model.content.delta", [e.type for e in events]
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

    def test_resume_does_not_replay_waiting_user_request(self) -> None:
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

            first = anyio.run(runtime.run_once, "read something")
            before = anyio.run(runtime.events, first.run_id)
            resumed = anyio.run(runtime.resume, first.run_id)
            after = anyio.run(runtime.events, first.run_id)

            self.assertEqual(resumed.status, RunStatus.WAITING_USER)
            self.assertEqual(resumed.answer, "Which file?")
            self.assertEqual(len(after), len(before))

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


class StreamingTextClient:
    """Yields a content delta then a generation_end per scripted answer."""

    model = "streaming-model"

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls = 0

    async def stream(self, messages, tools, config, runtime=None):
        text = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        gen = f"gen-{self.calls}"
        yield InferenceContentDelta(
            generation_id=gen,
            seq=1,
            run_id=config.run_id,
            delta=text,
        )
        message = InferenceMessage(role=InferenceRole.ASSISTANT, content=text)
        yield InferenceGenerationCompleted(
            generation_id=gen,
            seq=2,
            run_id=config.run_id,
            message=message,
        )


class _Collector:
    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, event) -> None:
        self.events.append(event)


class StreamingRuntimeTests(unittest.TestCase):
    def _runtime(self, workspace: str, client):
        approvals = MemoryApprovalService()
        registry = create_default_registry(Path(workspace))
        broker = ToolBroker(registry, PolicyEngine(approvals))
        return build_memory_runtime(
            inference_client=client,
            inference_config=InferenceConfig(),
            run_store=MemoryRunStore(),
            event_store=MemoryEventStore(),
            approvals=approvals,
            tool_broker=broker,
        )

    def test_run_streaming_forwards_runtime_event_projection(self) -> None:
        async def scenario():
            collector = _Collector()
            with tempfile.TemporaryDirectory() as workspace:
                runtime = self._runtime(workspace, StreamingTextClient(["Hello there"]))
                result = await runtime.run_streaming("hi", collector)
            return result, collector.events

        result, collected = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [event.type for event in collected]
        self.assertIn("model.content.delta", types)
        self.assertIn("model.completed", types)
        self.assertNotIn("inference.content.delta", types)
        self.assertTrue(all(not event.type.startswith("inference.") for event in collected))
        deltas = [event.delta for event in collected if event.type == "model.content.delta"]
        self.assertEqual(deltas, ["Hello there"])

    def test_run_streaming_forwards_tool_lifecycle(self) -> None:
        async def scenario():
            collector = _Collector()
            with tempfile.TemporaryDirectory() as workspace:
                Path(workspace, "fact.txt").write_text("ok", encoding="utf-8")
                client = ScriptedInferenceClient(
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
                            role=InferenceRole.ASSISTANT, content="done"
                        ),
                    ]
                )
                runtime = self._runtime(workspace, client)
                result = await runtime.run_streaming("read it", collector)
            return result, collector.events

        result, collected = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [event.type for event in collected]
        self.assertIn("tool.started", types)
        self.assertIn("tool.completed", types)

    def test_run_streaming_keeps_multi_turn_memory(self) -> None:
        async def scenario():
            with tempfile.TemporaryDirectory() as workspace:
                runtime = self._runtime(
                    workspace, StreamingTextClient(["first answer", "second answer"])
                )
                collector = _Collector()
                first = await runtime.run_streaming("question one", collector)
                second = await runtime.run_streaming(
                    "question two", collector, run_id=first.run_id
                )
                events = await runtime.events(first.run_id)
            return first, second, events

        first, second, events = anyio.run(scenario)
        self.assertEqual(first.run_id, second.run_id)
        contents = [
            m.content for m in reconstruct_messages_from_events(events)
        ]
        self.assertIn("question one", contents)
        self.assertIn("first answer", contents)
        self.assertIn("question two", contents)

    def test_run_streaming_answers_ask_user(self) -> None:
        async def scenario():
            with tempfile.TemporaryDirectory() as workspace:
                client = ScriptedInferenceClient(
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
                        ),
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT, content="Got it"
                        ),
                    ]
                )
                runtime = self._runtime(workspace, client)
                collector = _Collector()
                waiting = await runtime.run_streaming("start", collector)
                answered = await runtime.run_streaming(
                    "fact.txt", collector, run_id=waiting.run_id
                )
            return waiting, answered

        waiting, answered = anyio.run(scenario)
        self.assertEqual(waiting.status, RunStatus.WAITING_USER)
        self.assertEqual(answered.status, RunStatus.SUCCEEDED)
        self.assertEqual(answered.answer, "Got it")


if __name__ == "__main__":
    unittest.main()
