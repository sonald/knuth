import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.messages import InferenceMessage, InferenceRole
from knuth_llmd import (
    Config,
    InferenceConfig,
    InferenceEventType,
    LiteLLMInferenceClient,
    load_config,
)


class ConfigTests(unittest.TestCase):
    def test_load_config_reads_local_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "config.toml")
            config_path.write_text(
                "\n".join(
                    [
                        'api_key = "test-key"',
                        'base_url = "https://example.test/v1"',
                        'model = "test-model"',
                        "timeout = 45.5",
                    ]
                ),
                encoding="utf-8",
            )

            config = anyio.run(load_config, config_path, {})

            self.assertIsInstance(config, Config)
            self.assertEqual(config.api_key, "test-key")
            self.assertEqual(config.base_url, "https://example.test/v1")
            self.assertEqual(config.model, "test-model")
            self.assertEqual(config.timeout, 45.5)

    def test_environment_values_override_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "config.toml")
            config_path.write_text(
                "\n".join(
                    [
                        'api_key = "file-key"',
                        'base_url = "https://file.test/v1"',
                        'model = "file-model"',
                        "timeout = 30",
                    ]
                ),
                encoding="utf-8",
            )

            config = anyio.run(
                load_config,
                config_path,
                {
                    "KNUTH_API_KEY": "env-key",
                    "KNUTH_BASE_URL": "https://env.test/v1",
                    "KNUTH_MODEL": "env-model",
                    "KNUTH_TIMEOUT": "90.5",
                },
            )

            self.assertEqual(config.api_key, "env-key")
            self.assertEqual(config.base_url, "https://env.test/v1")
            self.assertEqual(config.model, "env-model")
            self.assertEqual(config.timeout, 90.5)

    def test_load_config_fails_when_required_values_are_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "KNUTH_API_KEY"):
            anyio.run(load_config, Path("does-not-exist.toml"), {})


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
                    config=InferenceConfig(run_id="run-1"),
                )
            ]

        events = anyio.run(collect)

        self.assertEqual(events[0].type, InferenceEventType.GENERATION_START)
        self.assertEqual(events[0].payload["model"], "test-model")
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
                    config=InferenceConfig(),
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
                    config=InferenceConfig(),
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
