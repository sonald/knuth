import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.messages import InferenceMessage, InferenceRole
from knuth_llmd import (
    InferenceConfig,
    InferenceEventType,
    LiteLLMInferenceClient,
    ToolSpec,
    load_llm_config,
    tool_spec_to_payload,
)


class CapturingCompletion:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.kwargs: dict[str, object] | None = None
        self._response = response or {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "real response",
                    }
                }
            ]
        }

    async def __call__(self, **kwargs: object) -> dict[str, object]:
        self.kwargs = kwargs
        return self._response


class LlmConfigTests(unittest.TestCase):
    def test_load_llm_config_reads_knuth_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir, ".env")
            env_path.write_text(
                "\n".join(
                    [
                        "KNUTH_API_KEY=test-key",
                        "KNUTH_BASE_URL=https://example.test/v1",
                        "KNUTH_MODEL=test-model",
                    ]
                ),
                encoding="utf-8",
            )

            config = anyio.run(load_llm_config, env_path, {})

            self.assertEqual(config.api_key, "test-key")
            self.assertEqual(config.base_url, "https://example.test/v1")
            self.assertEqual(config.model, "test-model")

    def test_environment_values_override_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir, ".env")
            env_path.write_text(
                "\n".join(
                    [
                        "KNUTH_API_KEY=file-key",
                        "KNUTH_BASE_URL=https://file.test/v1",
                        "KNUTH_MODEL=file-model",
                    ]
                ),
                encoding="utf-8",
            )

            config = anyio.run(
                load_llm_config,
                env_path,
                {
                    "KNUTH_API_KEY": "env-key",
                    "KNUTH_BASE_URL": "https://env.test/v1",
                    "KNUTH_MODEL": "env-model",
                },
            )

            self.assertEqual(config.api_key, "env-key")
            self.assertEqual(config.base_url, "https://env.test/v1")
            self.assertEqual(config.model, "env-model")

    def test_load_llm_config_fails_when_required_values_are_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "KNUTH_API_KEY"):
            anyio.run(load_llm_config, Path("does-not-exist.env"), {})


class LiteLLMInferenceClientCompleteTests(unittest.TestCase):
    def test_complete_calls_litellm_completion_and_parses_response(self) -> None:
        completion = CapturingCompletion()
        client = LiteLLMInferenceClient(
            model="test-model",
            base_url="https://example.test/v1",
            api_key="test-key",
            completion_fn=completion,
            timeout=12.5,
        )

        response = anyio.run(
            client.complete,
            [InferenceMessage(role=InferenceRole.USER, content="hello")],
            InferenceConfig(model="test-model", timeout_s=12.5),
        )

        self.assertEqual(response.message.content, "real response")
        self.assertIsNotNone(completion.kwargs)
        kwargs = completion.kwargs or {}
        self.assertEqual(kwargs["model"], "openai/test-model")
        self.assertEqual(kwargs["base_url"], "https://example.test/v1")
        self.assertEqual(kwargs["api_key"], "test-key")
        self.assertEqual(kwargs["timeout"], 12.5)
        self.assertEqual(kwargs["parallel_tool_calls"], False)
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "hello"}])
        self.assertNotIn("tools", kwargs)

    def test_complete_does_not_double_prefix_provider_model(self) -> None:
        completion = CapturingCompletion()
        client = LiteLLMInferenceClient(
            model="openai/test-model",
            base_url="https://example.test/v1",
            api_key="test-key",
            completion_fn=completion,
        )

        anyio.run(
            client.complete,
            [InferenceMessage(role=InferenceRole.USER, content="hello")],
            InferenceConfig(model="openai/test-model"),
        )

        kwargs = completion.kwargs or {}
        self.assertEqual(kwargs["model"], "openai/test-model")

    def test_complete_includes_tool_specs_when_available(self) -> None:
        completion = CapturingCompletion()
        client = LiteLLMInferenceClient(
            model="test-model",
            base_url="https://example.test/v1",
            api_key="test-key",
            completion_fn=completion,
        )

        anyio.run(
            client.complete,
            [InferenceMessage(role=InferenceRole.USER, content="read file")],
            InferenceConfig(model="test-model"),
            [
                tool_spec_to_payload(
                    ToolSpec(
                        name="read_file",
                        description="Read a file",
                        input_schema={
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    )
                )
            ],
        )

        kwargs = completion.kwargs or {}
        self.assertEqual(
            kwargs["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
        )
        self.assertEqual(kwargs["tool_choice"], "auto")

    def test_complete_parses_tool_calls(self) -> None:
        completion = CapturingCompletion(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{\"path\": \"README.md\"}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
        client = LiteLLMInferenceClient(
            model="test-model",
            base_url="https://example.test/v1",
            api_key="test-key",
            completion_fn=completion,
        )

        response = anyio.run(
            client.complete,
            [InferenceMessage(role=InferenceRole.USER, content="read README")],
            InferenceConfig(model="test-model"),
        )

        self.assertEqual(response.message.content, "")
        self.assertEqual(response.message.role, InferenceRole.ASSISTANT)
        self.assertEqual(
            [(call.id, call.name, call.arguments) for call in response.message.tool_calls],
            [("call-1", "read_file", {"path": "README.md"})],
        )


class AsyncChunks:
    def __init__(self, chunks: list[dict[str, object]]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk


class CapturingStreamCompletion:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def __call__(self, **kwargs: object) -> AsyncChunks:
        self.kwargs = kwargs
        return AsyncChunks(
            [
                {"choices": [{"delta": {"content": "hello "}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "{\"path\":\"README.md\"}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ]
        )


class LiteLLMInferenceClientTests(unittest.TestCase):
    def test_stream_normalizes_litellm_chunks_into_inference_events(self) -> None:
        completion = CapturingStreamCompletion()
        client = LiteLLMInferenceClient(
            model="test-model",
            base_url="https://example.test/v1",
            api_key="test-key",
            completion_fn=completion,
        )

        async def collect():
            return [
                event
                async for event in client.stream(
                    messages=[InferenceMessage(role=InferenceRole.USER, content="hi")],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "description": "Read",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    config=InferenceConfig(model="test-model", run_id="run-1"),
                )
            ]

        events = anyio.run(collect)

        self.assertEqual(events[0].type, InferenceEventType.GENERATION_START)
        self.assertIn(InferenceEventType.CONTENT_DELTA, [event.type for event in events])
        tool_events = [event for event in events if event.type == InferenceEventType.TOOL_CALL]
        self.assertEqual(tool_events[0].payload["tool_call"]["name"], "read_file")
        self.assertEqual(events[-1].type, InferenceEventType.GENERATION_END)
        self.assertIsNotNone(completion.kwargs)
        kwargs = completion.kwargs or {}
        self.assertEqual(kwargs["stream"], True)
        self.assertEqual(kwargs["parallel_tool_calls"], False)
        self.assertEqual(kwargs["tool_choice"], "auto")

    def test_complete_uses_inference_messages_without_tools(self) -> None:
        completion = CapturingCompletion()
        client = LiteLLMInferenceClient(
            model="test-model",
            base_url="https://example.test/v1",
            api_key="test-key",
            completion_fn=completion,
        )

        result = anyio.run(
            client.complete,
            [InferenceMessage(role=InferenceRole.USER, content="hello")],
            InferenceConfig(model="test-model"),
        )

        self.assertEqual(result.message.content, "real response")
        kwargs = completion.kwargs or {}
        self.assertNotIn("tools", kwargs)
        self.assertEqual(kwargs["parallel_tool_calls"], False)


if __name__ == "__main__":
    unittest.main()
