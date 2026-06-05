import unittest

from knuth.core.events import RuntimeEvent
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall


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
                    id="call-1",
                    name="read_file",
                    arguments={"path": "README.md"},
                )
            ],
        )

        payload = message.to_litellm_message()

        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["tool_calls"][0]["id"], "call-1")
        self.assertIn('"README.md"', payload["tool_calls"][0]["function"]["arguments"])

    def test_runtime_event_allows_forward_compatible_extra_fields(self) -> None:
        event = RuntimeEvent(
            id="evt-1",
            run_id="run-1",
            seq=1,
            namespace="model",
            name="completed",
            type="model.completed",
            created_at="2026-06-05T00:00:00Z",
            future_field="kept",
        )

        self.assertEqual(event.schema_version, "v0")
        self.assertEqual(event.__pydantic_extra__["future_field"], "kept")


if __name__ == "__main__":
    unittest.main()
