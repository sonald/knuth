import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.messages import InferenceMessage, InferenceRole
from knuth_llmd import (
    InferenceConfig,
    InferenceEventType,
    LiteLLMInferenceClient,
    load_llm_config,
)


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


class AsyncChunks:
    def __init__(self, chunks: list[dict[str, object]]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk


class CapturingStreamCompletion:
    def __init__(self, chunks: list[dict[str, object]] | None = None) -> None:
        self.kwargs: dict[str, object] | None = None
        self._chunks = chunks or [
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

    async def __call__(self, **kwargs: object) -> AsyncChunks:
        self.kwargs = kwargs
        return AsyncChunks(self._chunks)


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
        self.assertEqual(kwargs["model"], "openai/test-model")
        self.assertEqual(kwargs["base_url"], "https://example.test/v1")
        self.assertEqual(kwargs["api_key"], "test-key")
        self.assertEqual(kwargs["parallel_tool_calls"], False)
        self.assertEqual(kwargs["tool_choice"], "auto")

    def test_stream_splits_inline_think_tags_into_reasoning(self) -> None:
        # Reasoning arrives inline in `content` wrapped in <think> tags, with the
        # closing tag split across two chunks to exercise the buffered splitter.
        completion = CapturingStreamCompletion(
            [
                {"choices": [{"delta": {"content": "<think>17*23="}}]},
                {"choices": [{"delta": {"content": "391</thi"}}]},
                {"choices": [{"delta": {"content": "nk>The answer is 391."}}]},
            ]
        )
        client = LiteLLMInferenceClient(
            model="test-model", completion_fn=completion
        )

        async def collect():
            return [
                event
                async for event in client.stream(
                    messages=[InferenceMessage(role=InferenceRole.USER, content="hi")],
                    tools=[],
                    config=InferenceConfig(model="test-model"),
                )
            ]

        events = anyio.run(collect)

        reasoning = "".join(
            e.payload["delta"]
            for e in events
            if e.type == InferenceEventType.REASONING_DELTA
        )
        content = "".join(
            e.payload["delta"]
            for e in events
            if e.type == InferenceEventType.CONTENT_DELTA
        )
        self.assertEqual(reasoning, "17*23=391")
        self.assertEqual(content, "The answer is 391.")
        # The materialized assistant message must not leak think tags.
        self.assertEqual(
            events[-1].payload["message"]["content"], "The answer is 391."
        )

    def test_stream_uses_inference_messages_without_tools(self) -> None:
        completion = CapturingStreamCompletion(
            [{"choices": [{"delta": {"content": "real response"}}]}]
        )
        client = LiteLLMInferenceClient(
            model="openai/test-model",
            base_url="https://example.test/v1",
            api_key="test-key",
            completion_fn=completion,
        )

        async def collect():
            return [
                event
                async for event in client.stream(
                    messages=[InferenceMessage(role=InferenceRole.USER, content="hello")],
                    tools=[],
                    config=InferenceConfig(model="openai/test-model"),
                )
            ]

        events = anyio.run(collect)

        self.assertEqual(events[-1].payload["message"]["content"], "real response")
        kwargs = completion.kwargs or {}
        self.assertNotIn("tools", kwargs)
        self.assertEqual(kwargs["parallel_tool_calls"], False)
        self.assertEqual(kwargs["model"], "openai/test-model")


if __name__ == "__main__":
    unittest.main()
