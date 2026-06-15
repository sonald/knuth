"""End-to-end spike test: a scripted run streamed through the AG-UI endpoint.

Drives a realistic two-step run (reason -> call read_file -> answer) and asserts
the SSE output is a well-formed AG-UI event sequence. This validates both the
event translation and the FastAPI/anyio streaming path without a live model.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import anyio
from fastapi.testclient import TestClient

from knuth.core.events import (
    InferenceContentDelta,
    InferenceGenerationCompleted,
    InferenceReasoningCompleted,
    InferenceReasoningDelta,
)
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth_cli.tools import create_cli_tool_provider
from knuth_llmd import InferenceConfig
from knuth_runtime import MemoryRunLedger, build_memory_runtime
from knuth_toold import ToolBroker, create_default_registry
from knuth_runtime.policy import PolicyEngine

from knuth_agui import create_agui_client_tool_provider, create_app


def _tool_name(tool: object) -> str | None:
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        return name if isinstance(name, str) else None
    name = tool.get("name")
    return name if isinstance(name, str) else None


class _ScriptedSpikeClient:
    """Step 1: reason, then call read_file. Step 2: stream the answer."""

    model = "scripted-spike-model"

    def __init__(self, fact_path: str) -> None:
        self._fact_path = fact_path
        self.calls = 0
        self.seen_tool_names: list[list[str]] = []

    async def stream(self, messages, tools, config, runtime=None):
        self.calls += 1
        self.seen_tool_names.append(
            [name for tool in tools if (name := _tool_name(tool)) is not None]
        )
        gen = f"gen-{self.calls}"

        def started(seq):
            from knuth.core.events import InferenceGenerationStarted

            return InferenceGenerationStarted(
                generation_id=gen, seq=seq, run_id=config.run_id, model=self.model
            )

        if self.calls == 1:
            yield started(1)
            yield InferenceReasoningDelta(
                generation_id=gen, seq=2, run_id=config.run_id, delta="I should read the file."
            )
            yield InferenceReasoningCompleted(generation_id=gen, seq=3, run_id=config.run_id)
            message = InferenceMessage(
                role=InferenceRole.ASSISTANT,
                content="",
                tool_calls=[
                    CoreToolCall(
                        tool_call_id="call-1",
                        name="read_file",
                        arguments={"path": self._fact_path},
                    )
                ],
            )
            yield InferenceGenerationCompleted(
                generation_id=gen, seq=4, run_id=config.run_id, message=message
            )
            return

        yield started(1)
        for i, piece in enumerate(["The file ", "says hello."], start=2):
            yield InferenceContentDelta(
                generation_id=gen, seq=i, run_id=config.run_id, delta=piece
            )
        yield InferenceGenerationCompleted(
            generation_id=gen,
            seq=5,
            run_id=config.run_id,
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT, content="The file says hello."
            ),
        )


class _ScriptedClientToolClient:
    """Step 1 asks for a browser client tool; step 2 answers from its result."""

    model = "scripted-client-tool-model"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_tool_names: list[list[str]] = []

    async def stream(self, messages, tools, config, runtime=None):
        self.calls += 1
        self.seen_tool_names.append(
            [name for tool in tools if (name := _tool_name(tool)) is not None]
        )
        gen = f"client-tool-gen-{self.calls}"

        def started(seq):
            from knuth.core.events import InferenceGenerationStarted

            return InferenceGenerationStarted(
                generation_id=gen, seq=seq, run_id=config.run_id, model=self.model
            )

        yield started(1)
        if self.calls == 1:
            yield InferenceGenerationCompleted(
                generation_id=gen,
                seq=2,
                run_id=config.run_id,
                message=InferenceMessage(
                    role=InferenceRole.ASSISTANT,
                    content="",
                    tool_calls=[
                        CoreToolCall(
                            tool_call_id="client-call-1",
                            name="browser_context",
                            arguments={},
                        )
                    ],
                ),
            )
            return

        tool_result = next(
            (
                message.content or ""
                for message in reversed(messages)
                if message.role == InferenceRole.TOOL_RESULT
            ),
            "",
        )
        answer = f"Browser context received: {tool_result}"
        yield InferenceContentDelta(
            generation_id=gen, seq=2, run_id=config.run_id, delta=answer
        )
        yield InferenceGenerationCompleted(
            generation_id=gen,
            seq=3,
            run_id=config.run_id,
            message=InferenceMessage(role=InferenceRole.ASSISTANT, content=answer),
        )


class _ScriptedApprovalClient:
    """Ask for a dangerous server tool so the runtime waits for approval."""

    model = "scripted-approval-model"

    async def stream(self, messages, tools, config, runtime=None):
        from knuth.core.events import InferenceGenerationStarted

        gen = "approval-gen-1"
        yield InferenceGenerationStarted(
            generation_id=gen, seq=1, run_id=config.run_id, model=self.model
        )
        yield InferenceGenerationCompleted(
            generation_id=gen,
            seq=2,
            run_id=config.run_id,
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT,
                content="",
                tool_calls=[
                    CoreToolCall(
                        tool_call_id="shell-call-1",
                        name="shell",
                        arguments={"command": "pwd"},
                    )
                ],
            ),
        )


_BROWSER_CONTEXT_TOOL = {
    "name": "browser_context",
    "description": "Return current browser context.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def _collect_events(response_text: str) -> list[dict]:
    events = []
    for block in response_text.strip().split("\n\n"):
        block = block.strip()
        if block.startswith("data:"):
            events.append(json.loads(block[len("data:") :].strip()))
    return events


class AGUIM2Tests(unittest.TestCase):
    def _runtime(self, workspace: str, ledger: MemoryRunLedger | None = None):
        fact_path = Path(workspace, "fact.txt")
        fact_path.write_text("hello", encoding="utf-8")
        registry = create_default_registry()
        return build_memory_runtime(
            inference_client=_ScriptedSpikeClient(str(fact_path)),
            inference_config=InferenceConfig(),
            ledger=ledger or MemoryRunLedger(),
            tool_broker=ToolBroker(registry, PolicyEngine()),
        )

    def _cli_runtime(self, workspace: str):
        fact_path = Path(workspace, "fact.txt")
        fact_path.write_text("hello", encoding="utf-8")
        client = _ScriptedSpikeClient(str(fact_path))
        runtime = build_memory_runtime(
            inference_client=client,
            inference_config=InferenceConfig(),
            ledger=MemoryRunLedger(),
            tool_providers=[create_cli_tool_provider()],
            include_default_tools=True,
        )
        return runtime, client

    def _client_tool_runtime(self):
        scripted_client = _ScriptedClientToolClient()
        client_tool_provider = create_agui_client_tool_provider()
        runtime = build_memory_runtime(
            inference_client=scripted_client,
            inference_config=InferenceConfig(),
            ledger=MemoryRunLedger(),
            tool_providers=[client_tool_provider],
            include_default_tools=False,
        )
        return runtime, scripted_client, client_tool_provider

    def _approval_runtime(self):
        return build_memory_runtime(
            inference_client=_ScriptedApprovalClient(),
            inference_config=InferenceConfig(),
            ledger=MemoryRunLedger(),
            tool_providers=[create_cli_tool_provider()],
            include_default_tools=True,
        )

    def _post_agent(
        self, client: TestClient, body: dict, expected_status: int = 200
    ) -> list[dict]:
        response = client.post("/agent", json=body)
        self.assertEqual(response.status_code, expected_status, response.text)
        if expected_status != 200:
            return []
        return _collect_events(response.text)

    def test_stream_is_wellformed_agui_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            app = create_app(self._runtime(workspace))
            with TestClient(app) as client:
                events = self._post_agent(
                    client,
                    {
                        "threadId": "run_t_1",
                        "messages": [
                            {"role": "user", "content": "what does the file say?"}
                        ],
                    },
                )
        types = [e["type"] for e in events]

        self.assertEqual(types[0], "RUN_STARTED")
        self.assertEqual(types[-1], "RUN_FINISHED")
        self.assertEqual(events[0]["threadId"], "run_t_1")
        self.assertEqual(events[0]["runId"], "run_t_1")

        # thinking region is bracketed
        self.assertIn("THINKING_START", types)
        self.assertIn("THINKING_TEXT_MESSAGE_CONTENT", types)
        self.assertLess(types.index("THINKING_START"), types.index("THINKING_END"))

        # tool call: start -> args -> end -> result, all for call-1
        self.assertIn("TOOL_CALL_START", types)
        start_i = types.index("TOOL_CALL_START")
        self.assertEqual(events[start_i]["toolCallName"], "read_file")
        self.assertEqual(events[start_i]["toolCallId"], "call-1")
        args_event = events[types.index("TOOL_CALL_ARGS")]
        self.assertEqual(json.loads(args_event["delta"])["path"][-8:], "fact.txt")
        result_event = events[types.index("TOOL_CALL_RESULT")]
        self.assertEqual(result_event["toolCallId"], "call-1")
        self.assertIn("hello", result_event["content"])
        self.assertLess(types.index("TOOL_CALL_START"), types.index("TOOL_CALL_END"))
        self.assertLess(types.index("TOOL_CALL_END"), types.index("TOOL_CALL_RESULT"))

        # streamed answer is bracketed start -> content -> end
        ts = types.index("TEXT_MESSAGE_START")
        te = types.index("TEXT_MESSAGE_END")
        self.assertLess(ts, types.index("TEXT_MESSAGE_CONTENT"))
        self.assertLess(types.index("TEXT_MESSAGE_CONTENT"), te)
        message_ids = {
            events[ts]["messageId"],
            events[types.index("TEXT_MESSAGE_CONTENT")]["messageId"],
            events[te]["messageId"],
        }
        self.assertEqual(len(message_ids), 1)  # same id across the message lifecycle

        # tool call comes before the final answer
        self.assertLess(types.index("TOOL_CALL_RESULT"), ts)

    def test_stream_can_execute_cli_read_file_tool_provider(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            runtime, scripted_client = self._cli_runtime(workspace)
            app = create_app(runtime)
            with TestClient(app) as client:
                events = self._post_agent(
                    client,
                    {
                        "threadId": "run_cli_tool_1",
                        "messages": [{"role": "user", "content": "read the fact"}],
                    },
                )
        types = [event["type"] for event in events]
        self.assertIn("read_file", scripted_client.seen_tool_names[0])
        self.assertIn("TOOL_CALL_START", types)
        self.assertIn("TOOL_CALL_RESULT", types)
        self.assertIn("hello", events[types.index("TOOL_CALL_RESULT")]["content"])

    def test_client_tool_round_trip_waits_for_tool_result_then_resumes(self) -> None:
        runtime, scripted_client, client_tool_provider = self._client_tool_runtime()
        app = create_app(runtime, client_tool_provider=client_tool_provider)
        run_id = "run_client_tool_1"
        with TestClient(app) as client:
            first = self._post_agent(
                client,
                {
                    "threadId": run_id,
                    "messages": [
                        {"role": "user", "content": "use the browser context"}
                    ],
                    "tools": [_BROWSER_CONTEXT_TOOL],
                },
            )
            first_types = [event["type"] for event in first]
            self.assertIn("browser_context", scripted_client.seen_tool_names[0])
            self.assertIn("TOOL_CALL_START", first_types)
            self.assertIn("CUSTOM", first_types)
            custom = next(
                event
                for event in first
                if event["type"] == "CUSTOM"
                and event["name"] == "knuth.tool_result_required"
            )
            self.assertEqual(custom["value"]["runId"], run_id)
            self.assertEqual(custom["value"]["toolCallId"], "client-call-1")
            self.assertEqual(custom["value"]["toolName"], "browser_context")

            threads = client.get("/threads").json()["threads"]
            self.assertEqual(threads[0]["status"], "waiting_tool_result")

            tool_result = client.post(
                "/tool_result",
                json={
                    "runId": run_id,
                    "toolCallId": "client-call-1",
                    "result": {"href": "http://localhost:3000/", "locale": "en-US"},
                },
            )
            self.assertEqual(tool_result.status_code, 200, tool_result.text)
            self.assertEqual(tool_result.json()["status"], "succeeded")

            resumed = self._post_agent(
                client,
                {
                    "threadId": run_id,
                    "messages": [],
                    "tools": [_BROWSER_CONTEXT_TOOL],
                },
            )
            resumed_types = [event["type"] for event in resumed]
            self.assertEqual(scripted_client.calls, 2)
            self.assertIn("browser_context", scripted_client.seen_tool_names[1])
            self.assertIn("TEXT_MESSAGE_CONTENT", resumed_types)
            answer = "".join(
                event.get("delta", "")
                for event in resumed
                if event["type"] == "TEXT_MESSAGE_CONTENT"
            )
            self.assertIn("http://localhost:3000/", answer)

            threads = client.get("/threads").json()["threads"]
            self.assertEqual(threads[0]["status"], "succeeded")
            history = client.get(f"/threads/{run_id}/history").json()
            tool_messages = [
                message
                for message in history["messages"]
                if message["role"] == "tool"
            ]
            self.assertEqual(tool_messages[0]["toolCallId"], "client-call-1")
            self.assertIn("localhost:3000", tool_messages[0]["content"])

        global_tools = anyio.run(runtime.tools)
        self.assertIn(
            "browser_context",
            [name for tool in global_tools if (name := _tool_name(tool)) is not None],
        )

    def test_pending_approvals_endpoint_restores_approval_card_data(self) -> None:
        app = create_app(self._approval_runtime())
        run_id = "run_approval_restore_1"
        with TestClient(app) as client:
            events = self._post_agent(
                client,
                {
                    "threadId": run_id,
                    "messages": [{"role": "user", "content": "where am I?"}],
                },
            )
            custom = next(
                event
                for event in events
                if event["type"] == "CUSTOM"
                and event["name"] == "knuth.approval_requested"
            )
            response = client.get(f"/threads/{run_id}/approvals")

        self.assertEqual(response.status_code, 200, response.text)
        approvals = response.json()["approvals"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["approvalId"], custom["value"]["approvalId"])
        self.assertEqual(approvals[0]["toolCallId"], "shell-call-1")
        self.assertEqual(approvals[0]["title"], "Approve tool call: shell")
        self.assertEqual(approvals[0]["preview"]["tool"], "shell")
        self.assertEqual(approvals[0]["preview"]["args"]["command"], "pwd")

    def test_missing_thread_id_generates_canonical_run_id_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            app = create_app(self._runtime(workspace))
            with TestClient(app) as client:
                events = self._post_agent(
                    client,
                    {"messages": [{"role": "user", "content": "read the fact"}]},
                )
                thread_id = events[0]["threadId"]
                self.assertRegex(thread_id, r"^run_[A-Za-z0-9_-]{1,80}$")
                self.assertEqual(events[0]["runId"], thread_id)

                threads = client.get("/threads").json()["threads"]
                self.assertEqual(threads[0]["threadId"], thread_id)

                history = client.get(f"/threads/{thread_id}/history")
                self.assertEqual(history.status_code, 200)
                snapshot = history.json()
                self.assertEqual(snapshot["type"], "MESSAGES_SNAPSHOT")
                contents = [message.get("content") for message in snapshot["messages"]]
                self.assertIn("read the fact", contents)
                self.assertIn("The file says hello.", contents)

    def test_invalid_thread_id_is_rejected_before_runtime_start(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            app = create_app(self._runtime(workspace))
            with TestClient(app) as client:
                self._post_agent(
                    client,
                    {
                        "threadId": "../bad",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    expected_status=400,
                )

    def test_existing_succeeded_thread_continues_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            run_id = "run_continue_1"
            app = create_app(self._runtime(workspace))
            with TestClient(app) as client:
                self._post_agent(
                    client,
                    {
                        "threadId": run_id,
                        "messages": [{"role": "user", "content": "first question"}],
                    },
                )
                second = self._post_agent(
                    client,
                    {
                        "threadId": run_id,
                        "messages": [{"role": "user", "content": "second question"}],
                    },
                )
                self.assertEqual(second[0]["threadId"], run_id)
                self.assertEqual(second[0]["runId"], run_id)

                history = client.get(f"/threads/{run_id}/messages").json()
                contents = [message.get("content") for message in history["messages"]]
                self.assertIn("first question", contents)
                self.assertIn("second question", contents)

    def test_pause_transitions_created_run(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            ledger = MemoryRunLedger()

            async def seed() -> None:
                await ledger.create_run("idle", run_id="run_pause_1")

            anyio.run(seed)
            app = create_app(self._runtime(workspace, ledger=ledger))
            with TestClient(app) as client:
                response = client.post("/pause", json={"runId": "run_pause_1"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "paused")


if __name__ == "__main__":
    unittest.main()
