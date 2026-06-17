import unittest
from typing import get_args

from knuth.core.events import (
    MessageRewriteAnchor,
    MessageRewriteAnchorDraft,
    RunCancelled,
    RunCreated,
    RunInvocationEndedDraft,
    RunInvocationStartedDraft,
    ModelToolCallStartedDraft,
    StoredRuntimeEvent,
    TransientRuntimeEvent,
    emit_transient_runtime_event,
    parse_stored_runtime_event_json,
    store_runtime_event,
)
from knuth.core.runtime_events import RunCancelledDraft
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall
from knuth.core.types import EventDurability


class CoreModelTests(unittest.TestCase):
    def test_tool_result_message_converts_to_litellm_tool_message(self) -> None:
        message = InferenceMessage(
            role=InferenceRole.TOOL_RESULT,
            tool_call_id="call-1",
            tool_name="read_file",
            content="hello",
        )

        self.assertEqual(
            message.to_litellm_message(),
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "read_file",
                "content": "hello",
            },
        )

    def test_assistant_message_carries_tool_calls(self) -> None:
        message = InferenceMessage(
            role=InferenceRole.ASSISTANT,
            content=None,
            tool_calls=[
                ToolCall(
                    tool_call_id="call-1",
                    name="read_file",
                    arguments={"path": "README.md"},
                )
            ],
        )

        payload = message.to_litellm_message()

        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["tool_calls"][0]["id"], "call-1")
        self.assertIn('"README.md"', payload["tool_calls"][0]["function"]["arguments"])

    def test_typed_runtime_event_does_not_keep_extra_fields(self) -> None:
        event = RunCreated(
            id="evt-1",
            run_id="run-1",
            seq=1,
            type="run.created",
            query="hello",
            created_at="2026-06-05T00:00:00Z",
            future_field="kept",
        )

        self.assertEqual(event.schema_version, "v0")
        self.assertFalse(hasattr(event, "future_field"))
        self.assertIsNone(event.__pydantic_extra__)

    def test_run_invocation_events_are_transient_runtime_events(self) -> None:
        started = emit_transient_runtime_event(
            "run-1",
            RunInvocationStartedDraft(mode="start"),
            event_id="evt-started",
            created_at="2026-06-09T00:00:00Z",
        )
        ended = emit_transient_runtime_event(
            "run-1",
            RunInvocationEndedDraft(mode="start", status="succeeded"),
            event_id="evt-ended",
            created_at="2026-06-09T00:00:01Z",
        )

        self.assertEqual(started.type, "run.invocation.started")
        self.assertEqual(started.mode, "start")
        self.assertEqual(started.durability, EventDurability.TRANSIENT)
        self.assertFalse(hasattr(started, "seq"))
        self.assertEqual(ended.type, "run.invocation.ended")
        self.assertEqual(ended.status, "succeeded")

    def test_tool_call_effective_id_falls_back_to_position(self) -> None:
        self.assertEqual(ToolCall(tool_call_id="call-1", name="t").effective_id, "call-1")
        self.assertEqual(ToolCall(name="t", index=2).effective_id, "call_2")

    def test_stored_event_round_trips_to_its_own_class(self) -> None:
        # Regression: run.paused and run.cancelled share the same field shape;
        # without a type-discriminated union the JSON round trip used to pick
        # the structurally-first class (RunPaused) for a run.cancelled event.
        stored = store_runtime_event(
            "run-1",
            3,
            RunCancelledDraft(reason="user hit ctrl-c"),
            event_id="evt-cancel",
            created_at="2026-06-11T00:00:00Z",
        )

        parsed = parse_stored_runtime_event_json(stored.model_dump_json())

        self.assertIsInstance(parsed, RunCancelled)
        self.assertEqual(parsed, stored)

    def test_message_rewrite_event_round_trips_to_its_own_class(self) -> None:
        stored = store_runtime_event(
            "run-1",
            4,
            MessageRewriteAnchorDraft(
                rewrite_id="rewrite-1",
                kind="begin",
                middleware="context_compaction",
                operation="replace",
                suppresses=["m:2"],
            ),
            event_id="evt-rewrite",
            created_at="2026-06-16T00:00:00Z",
        )

        parsed = parse_stored_runtime_event_json(stored.model_dump_json())

        self.assertIsInstance(parsed, MessageRewriteAnchor)
        self.assertEqual(parsed, stored)

    def test_every_union_member_declares_a_unique_type_tag(self) -> None:
        for union in (StoredRuntimeEvent, TransientRuntimeEvent):
            tags = [cls.model_fields["type"].default for cls in get_args(union)]
            self.assertNotIn(None, tags)
            self.assertEqual(len(tags), len(set(tags)))

    def test_model_tool_call_started_keeps_event_id_and_tool_call_id_separate(self) -> None:
        event = emit_transient_runtime_event(
            "run-1",
            ModelToolCallStartedDraft(index=0, tool_call_id="call-1"),
            event_id="evt-tool-started",
            created_at="2026-06-09T00:00:02Z",
        )

        self.assertEqual(event.id, "evt-tool-started")
        self.assertEqual(event.tool_call_id, "call-1")


if __name__ == "__main__":
    unittest.main()
