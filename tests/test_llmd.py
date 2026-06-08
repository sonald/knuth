import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
import platformdirs

from knuth.core.events import (
    InferenceContentDelta,
    InferenceGenerationCompleted,
    InferenceGenerationStarted,
    InferenceReasoningCompleted,
    InferenceReasoningDelta,
    InferenceToolCallCompleted,
    InferenceToolCallDelta,
    InferenceToolCallStarted,
)
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth_llmd import (
    Config,
    InferenceConfig,
    LiteLLMInferenceClient,
    load_config,
)
from knuth_llmd.config import default_config_path


def _write_yaml(path: Path, values: dict[str, object]) -> None:
    lines = []
    for key, value in values.items():
        if isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines), encoding="utf-8")


class ConfigPathTests(unittest.TestCase):
    def test_default_config_path_lives_in_user_data_dir(self) -> None:
        path = default_config_path()

        expected_parent = Path(platformdirs.user_data_dir("knuth")) / "llmd"
        self.assertEqual(path.parent, expected_parent)
        self.assertEqual(path.name, "knuth.yaml")


class ConfigTests(unittest.TestCase):
    def test_load_config_reads_yaml_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            _write_yaml(
                config_path,
                {
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                    "timeout": 45.5,
                },
            )

            config = anyio.run(load_config, config_path, {})

            self.assertIsInstance(config, Config)
            self.assertEqual(config.api_key, "test-key")
            self.assertEqual(config.base_url, "https://example.test/v1")
            self.assertEqual(config.model, "test-model")
            self.assertEqual(config.timeout, 45.5)

    def test_environment_values_override_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "knuth.yaml")
            _write_yaml(
                config_path,
                {
                    "api_key": "file-key",
                    "base_url": "https://file.test/v1",
                    "model": "file-model",
                    "timeout": 30,
                },
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
            anyio.run(load_config, Path("does-not-exist.yaml"), {})

    def test_load_config_defaults_to_user_data_dir_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "llmd", "knuth.yaml")
            config_path.parent.mkdir(parents=True)
            _write_yaml(
                config_path,
                {
                    "api_key": "default-key",
                    "base_url": "https://default.test/v1",
                    "model": "default-model",
                },
            )

            with patch(
                "knuth_llmd.config.default_config_path",
                return_value=config_path,
            ):
                config = anyio.run(load_config, None, {})

            self.assertEqual(config.api_key, "default-key")
            self.assertEqual(config.base_url, "https://default.test/v1")
            self.assertEqual(config.model, "default-model")


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
    def test_stream_normalizes_litellm_chunks_into_typed_inference_events(self) -> None:
        completion = CapturingStreamCompletion(
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
                                            "name": "read_",
                                            "arguments": "{\"path\":",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "name": "file",
                                            "arguments": "\"README.md\"}",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
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

        self.assertIsInstance(events[0], InferenceGenerationStarted)
        self.assertEqual(events[0].type, "inference.generation.started")
        self.assertEqual(events[0].model, "test-model")
        self.assertTrue(any(isinstance(event, InferenceContentDelta) for event in events))
        started = [event for event in events if isinstance(event, InferenceToolCallStarted)]
        deltas = [event for event in events if isinstance(event, InferenceToolCallDelta)]
        completed = [event for event in events if isinstance(event, InferenceToolCallCompleted)]
        self.assertEqual(started[0].index, 0)
        self.assertEqual(started[0].id, "call-1")
        self.assertEqual([event.name_delta for event in deltas], ["read_", "file"])
        self.assertEqual(
            [event.arguments_json_delta for event in deltas],
            ["{\"path\":", "\"README.md\"}"],
        )
        self.assertEqual(completed[0].tool_call.name, "read_file")
        self.assertEqual(completed[0].tool_call.arguments, {"path": "README.md"})
        self.assertIsInstance(events[-1], InferenceGenerationCompleted)
        self.assertEqual(events[-1].message.tool_calls[0].name, "read_file")
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
            e.delta
            for e in events
            if isinstance(e, InferenceReasoningDelta)
        )
        content = "".join(
            e.delta
            for e in events
            if isinstance(e, InferenceContentDelta)
        )
        self.assertEqual(reasoning, "17*23=391")
        self.assertEqual(content, "The answer is 391.")
        self.assertTrue(any(isinstance(e, InferenceReasoningCompleted) for e in events))
        reasoning_done_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, InferenceReasoningCompleted)
        )
        content_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, InferenceContentDelta)
        )
        self.assertLess(reasoning_done_index, content_index)
        # The materialized assistant message must not leak think tags.
        self.assertEqual(events[-1].message.content, "The answer is 391.")

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

        self.assertIsInstance(events[-1], InferenceGenerationCompleted)
        self.assertEqual(events[-1].message.content, "real response")
        kwargs = completion.kwargs or {}
        self.assertNotIn("tools", kwargs)
        self.assertEqual(kwargs["parallel_tool_calls"], False)
        self.assertEqual(kwargs["model"], "openai/test-model")


if __name__ == "__main__":
    unittest.main()
