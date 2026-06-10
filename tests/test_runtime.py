import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from knuth.core.events import (
    InferenceContentDelta,
    InferenceGenerationCompleted,
    InferenceToolCallDelta,
    InferenceToolCallStarted,
    RunCreatedDraft,
)
from knuth.core.messages import (
    InferenceMessage,
    InferenceRole,
    SystemSection,
    SystemSectionSource,
    ToolCall as CoreToolCall,
)
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig
from knuth_runtime import (
    MemoryEventStore,
    MemoryRunStore,
    SQLiteStore,
    build_sqlite_runtime,
    build_memory_runtime,
)
from knuth_runtime.approval import MemoryApprovalService
from knuth_runtime.context import (
    StaticSectionProvider,
    assemble_preamble,
    reconstruct_messages_from_events,
)
from knuth_runtime.observation import RuntimeEventInterest, RuntimeObservationError
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
    def test_build_sqlite_runtime_does_not_pass_workspace_to_toold(self) -> None:
        with (
            patch("knuth_runtime.agent.create_default_registry") as create_registry,
        ):
            create_registry.return_value = create_default_registry(Path.cwd())

            with tempfile.TemporaryDirectory() as temp_dir:
                runtime = build_sqlite_runtime(
                    inference_client=object(),
                    inference_config=InferenceConfig(timeout_s=60.0),
                    db_path=Path(temp_dir, "knuth.db"),
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
            async def resume():
                async with runtime.resume(first.run_id) as session:
                    return await session.result()

            resumed = anyio.run(resume)

            self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
            self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
            self.assertEqual(Path(workspace, "x.txt").read_text(encoding="utf-8"), "hello")

    def test_denied_approval_resumes_and_informs_model(self) -> None:
        """A denied tool call must not deadlock the run: on resume the model
        receives a denied tool result and the run can complete."""
        with tempfile.TemporaryDirectory() as workspace:
            client = CapturingScriptedClient(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        tool_calls=[
                            CoreToolCall(
                                id="call-shell",
                                name="shell",
                                arguments={"command": "date"},
                            )
                        ],
                    ),
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content="Understood, I will not run that command.",
                    ),
                ]
            )
            approvals = MemoryApprovalService()
            registry = create_default_registry(Path(workspace))
            broker = ToolBroker(registry, PolicyEngine(approvals))
            runtime = build_memory_runtime(
                inference_client=client,
                inference_config=InferenceConfig(),
                run_store=MemoryRunStore(),
                event_store=MemoryEventStore(),
                approvals=approvals,
                tool_broker=broker,
            )

            first = anyio.run(runtime.run_once, "run date")
            self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
            pending = anyio.run(runtime.pending_approvals, first.run_id)
            self.assertEqual(len(pending), 1)
            anyio.run(runtime.deny, pending[0].id)

            async def resume():
                async with runtime.resume(first.run_id) as session:
                    return await session.result()

            resumed = anyio.run(resume)

            self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
            self.assertEqual(resumed.answer, "Understood, I will not run that command.")
            # The model's final turn must see the denial as a tool result.
            final_turn_messages = client.captured_messages[-1]
            tool_results = [
                message
                for message in final_turn_messages
                if message.role == InferenceRole.TOOL_RESULT
            ]
            self.assertTrue(tool_results)
            self.assertIn("denied", (tool_results[-1].content or "").lower())
            # No approval is left pending and the denial is durable.
            self.assertEqual(
                anyio.run(runtime.pending_approvals, first.run_id), []
            )
            events = anyio.run(runtime.events, first.run_id)
            denied = [
                event
                for event in events
                if event.type == "tool.completed" and event.outcome == "denied"
            ]
            self.assertTrue(denied)


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


class StreamingToolCallProjectionClient:
    model = "streaming-tool-call-model"

    async def stream(self, messages, tools, config, runtime=None):
        yield InferenceToolCallStarted(
            generation_id="gen-tool",
            seq=1,
            run_id=config.run_id,
            index=0,
            id="call-1",
        )
        yield InferenceToolCallDelta(
            generation_id="gen-tool",
            seq=2,
            run_id=config.run_id,
            index=0,
            id="call-1",
            name_delta="shell",
        )
        yield InferenceGenerationCompleted(
            generation_id="gen-tool",
            seq=3,
            run_id=config.run_id,
            message=InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
        )


class CapturingInferenceClient:
    """Records the messages handed to each ``stream`` call."""

    model = "capturing-model"

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls = 0
        self.captured_messages: list[list[InferenceMessage]] = []

    async def stream(self, messages, tools, config, runtime=None):
        self.captured_messages.append(list(messages))
        text = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        yield InferenceGenerationCompleted(
            generation_id=f"gen-{self.calls}",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(role=InferenceRole.ASSISTANT, content=text),
        )


class CapturingScriptedClient(ScriptedInferenceClient):
    """Scripts full assistant messages while recording inbound message lists."""

    def __init__(self, messages: list[InferenceMessage]) -> None:
        super().__init__(messages)
        self.captured_messages: list[list[InferenceMessage]] = []

    async def stream(self, messages, tools, config, runtime=None):
        self.captured_messages.append(list(messages))
        async for event in super().stream(messages, tools, config, runtime):
            yield event


class SystemPreambleTests(unittest.TestCase):
    def _runtime(self, workspace: str, client, section_providers):
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
            section_providers=section_providers,
        )

    def test_base_identity_delivered_as_leading_system_message(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            client = CapturingInferenceClient(["Hello"])
            runtime = self._runtime(
                workspace,
                client,
                [StaticSectionProvider(SystemSectionSource.BASE, "BASE")],
            )
            anyio.run(runtime.run_once, "hi")

        first_turn_messages = client.captured_messages[0]
        self.assertEqual(first_turn_messages[0].role, InferenceRole.SYSTEM)
        self.assertEqual(first_turn_messages[0].content, "BASE")

    def test_sections_composed_in_provider_injection_order(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            client = CapturingInferenceClient(["Hello"])
            runtime = self._runtime(
                workspace,
                client,
                [
                    StaticSectionProvider(SystemSectionSource.BASE, "BASE"),
                    StaticSectionProvider(SystemSectionSource.USER, "USER"),
                ],
            )
            anyio.run(runtime.run_once, "hi")

        first_turn_messages = client.captured_messages[0]
        self.assertEqual(first_turn_messages[0].role, InferenceRole.SYSTEM)
        self.assertEqual(first_turn_messages[0].content, "BASE\n\nUSER")
        # The preamble is a single leading system message, not one per section.
        system_messages = [
            m for m in first_turn_messages if m.role == InferenceRole.SYSTEM
        ]
        self.assertEqual(len(system_messages), 1)

    def test_no_system_message_when_all_sections_empty(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            client = CapturingInferenceClient(["Hello"])
            runtime = self._runtime(
                workspace,
                client,
                [StaticSectionProvider(SystemSectionSource.USER, None)],
            )
            anyio.run(runtime.run_once, "hi")

        first_turn_messages = client.captured_messages[0]
        self.assertTrue(
            all(m.role != InferenceRole.SYSTEM for m in first_turn_messages)
        )

    def test_preamble_present_on_every_turn(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace, "fact.txt").write_text("ok", encoding="utf-8")
            client = CapturingScriptedClient(
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
                    InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
                ]
            )
            runtime = self._runtime(
                workspace,
                client,
                [StaticSectionProvider(SystemSectionSource.BASE, "BASE")],
            )
            anyio.run(runtime.run_once, "read it")

        self.assertEqual(len(client.captured_messages), 2)
        for turn_messages in client.captured_messages:
            self.assertEqual(turn_messages[0].role, InferenceRole.SYSTEM)
            self.assertEqual(turn_messages[0].content, "BASE")


class AssemblePreambleTests(unittest.TestCase):
    def test_joins_sections_in_given_order(self) -> None:
        sections = [
            SystemSection(source=SystemSectionSource.USER, text="USER"),
            SystemSection(source=SystemSectionSource.BASE, text="BASE"),
        ]
        self.assertEqual(assemble_preamble(sections), "USER\n\nBASE")

    def test_skips_empty_section_text(self) -> None:
        sections = [
            SystemSection(source=SystemSectionSource.BASE, text="BASE"),
            SystemSection(source=SystemSectionSource.USER, text=""),
        ]
        self.assertEqual(assemble_preamble(sections), "BASE")

    def test_returns_none_when_no_sections(self) -> None:
        self.assertIsNone(assemble_preamble([]))


class _Collector:
    interest = RuntimeEventInterest.all()

    def __init__(self) -> None:
        self.events: list = []

    async def handle_event(self, event) -> None:
        self.events.append(event)


class _RequiredFailingListener:
    interest = RuntimeEventInterest.for_types("model.content.delta")
    required = True

    async def handle_event(self, event) -> None:
        raise RuntimeError("renderer failed")


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

    def test_run_session_forwards_runtime_event_projection(self) -> None:
        async def scenario():
            collector = _Collector()
            with tempfile.TemporaryDirectory() as workspace:
                runtime = self._runtime(workspace, StreamingTextClient(["Hello there"]))
                async with runtime.start("hi", listeners=[collector]) as session:
                    result = await session.result()
            return result, collector.events

        result, collected = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [event.type for event in collected]
        self.assertIn("model.content.delta", types)
        self.assertIn("model.completed", types)
        self.assertIn("run.invocation.started", types)
        self.assertIn("run.created", types)
        self.assertIn("user.message", types)
        self.assertIn("run.invocation.ended", types)
        self.assertNotIn("inference.content.delta", types)
        self.assertTrue(all(not event.type.startswith("inference.") for event in collected))
        deltas = [event.delta for event in collected if event.type == "model.content.delta"]
        self.assertEqual(deltas, ["Hello there"])

    def test_required_listener_failure_raises_observation_error_with_result(self) -> None:
        async def scenario():
            with tempfile.TemporaryDirectory() as workspace:
                runtime = self._runtime(workspace, StreamingTextClient(["Hello there"]))
                async with runtime.start(
                    "hi", listeners=[_RequiredFailingListener()]
                ) as session:
                    with self.assertRaises(RuntimeObservationError) as raised:
                        await session.result()
                    return raised.exception

        error = anyio.run(scenario)

        self.assertEqual(error.result.status, RunStatus.SUCCEEDED)
        self.assertEqual(error.result.answer, "Hello there")
        self.assertEqual(len(error.failures), 1)

    def test_run_session_projects_streamed_tool_call_start_without_id_collision(self) -> None:
        async def scenario():
            collector = _Collector()
            with tempfile.TemporaryDirectory() as workspace:
                runtime = self._runtime(workspace, StreamingToolCallProjectionClient())
                async with runtime.start("use a tool", listeners=[collector]) as session:
                    result = await session.result()
            return result, collector.events

        result, collected = anyio.run(scenario)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        started = [
            event for event in collected if event.type == "model.tool_call.started"
        ]
        deltas = [event for event in collected if event.type == "model.tool_call.delta"]
        self.assertEqual(started[0].id.startswith("evt_"), True)
        self.assertEqual(started[0].tool_call_id, "call-1")
        self.assertEqual(deltas[0].tool_call_id, "call-1")

    def test_run_session_forwards_tool_lifecycle(self) -> None:
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
                async with runtime.start("read it", listeners=[collector]) as session:
                    result = await session.result()
            return result, collector.events

        result, collected = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [event.type for event in collected]
        self.assertIn("tool.started", types)
        self.assertIn("tool.completed", types)

    def test_run_session_keeps_multi_turn_memory(self) -> None:
        async def scenario():
            with tempfile.TemporaryDirectory() as workspace:
                runtime = self._runtime(
                    workspace, StreamingTextClient(["first answer", "second answer"])
                )
                collector = _Collector()
                async with runtime.start("question one", listeners=[collector]) as session:
                    first = await session.result()
                async with runtime.continue_run(
                    first.run_id, "question two", listeners=[collector]
                ) as session:
                    second = await session.result()
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

if __name__ == "__main__":
    unittest.main()
