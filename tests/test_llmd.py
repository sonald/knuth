import os
import unittest
from unittest.mock import patch

import anyio

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
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall
from knuth_llmd import (
    InferenceConfig,
    LiteLLMInferenceClient,
)
from knuth_llmd.client import _import_litellm_preserving_knuth_env


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


class CapturingResponsesCompletion:
    def __init__(self, chunks: list[dict[str, object]]) -> None:
        self.kwargs: dict[str, object] | None = None
        self._chunks = chunks

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

    def test_chatgpt_provider_omits_api_credentials_and_token_limit(self) -> None:
        responses = CapturingResponsesCompletion(
            [{"type": "response.output_text.delta", "delta": "ok"}]
        )
        client = LiteLLMInferenceClient(
            model="chatgpt/gpt-5.3-codex",
            responses_fn=responses,
        )

        async def collect():
            return [
                event
                async for event in client.stream(
                    messages=[InferenceMessage(role=InferenceRole.USER, content="hi")],
                    tools=[],
                    config=InferenceConfig(max_output_tokens=12),
                )
            ]

        anyio.run(collect)

        kwargs = responses.kwargs or {}
        self.assertEqual(kwargs["model"], "chatgpt/gpt-5.3-codex")
        self.assertNotIn("api_key", kwargs)
        self.assertNotIn("base_url", kwargs)
        self.assertNotIn("max_tokens", kwargs)

    def test_chatgpt_provider_streams_through_responses_api(self) -> None:
        completion = CapturingStreamCompletion(
            [{"choices": [{"delta": {"content": "wrong path"}}]}]
        )
        responses = CapturingResponsesCompletion(
            [
                {"type": "response.output_text.delta", "delta": "ok"},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "call_id": "call-1",
                        "type": "function_call",
                        "name": "read_file",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": "{\"path\":",
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": "\"README.md\"}",
                },
                {"type": "response.completed", "response": {}},
            ]
        )
        client = LiteLLMInferenceClient(
            model="chatgpt/gpt-5.4-mini",
            completion_fn=completion,
            responses_fn=responses,
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
                    config=InferenceConfig(max_output_tokens=12),
                )
            ]

        events = anyio.run(collect)

        self.assertIsNone(completion.kwargs)
        kwargs = responses.kwargs or {}
        self.assertEqual(kwargs["model"], "chatgpt/gpt-5.4-mini")
        self.assertEqual(
            kwargs["input"], [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(kwargs["stream"], True)
        self.assertEqual(kwargs["no-log"], True)
        self.assertEqual(kwargs["parallel_tool_calls"], False)
        self.assertEqual(
            kwargs["tools"],
            [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read",
                    "parameters": {"type": "object"},
                }
            ],
        )
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertNotIn("api_key", kwargs)
        self.assertNotIn("base_url", kwargs)
        self.assertNotIn("max_tokens", kwargs)
        self.assertTrue(any(isinstance(event, InferenceContentDelta) for event in events))
        completed = [event for event in events if isinstance(event, InferenceToolCallCompleted)]
        self.assertEqual(completed[0].tool_call.tool_call_id, "fc_1")
        self.assertEqual(completed[0].tool_call.name, "read_file")
        self.assertEqual(completed[0].tool_call.arguments, {"path": "README.md"})
        self.assertEqual(completed[0].tool_call.raw["responses_call_id"], "call-1")

    def test_chatgpt_provider_reads_arguments_from_output_item_done(self) -> None:
        responses = CapturingResponsesCompletion(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "call_id": "call-1",
                        "type": "function_call",
                        "name": "shell",
                    },
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "call_id": "call-1",
                        "type": "function_call",
                        "name": "shell",
                        "arguments": "{\"command\":\"ls\"}",
                    },
                },
                {"type": "response.completed", "response": {}},
            ]
        )
        client = LiteLLMInferenceClient(
            model="chatgpt/gpt-5.3-codex-spark",
            responses_fn=responses,
        )

        async def collect():
            return [
                event
                async for event in client.stream(
                    messages=[InferenceMessage(role=InferenceRole.USER, content="ls")],
                    tools=[],
                    config=InferenceConfig(),
                )
            ]

        events = anyio.run(collect)

        completed = [event for event in events if isinstance(event, InferenceToolCallCompleted)]
        self.assertEqual(completed[0].tool_call.name, "shell")
        self.assertEqual(completed[0].tool_call.arguments, {"command": "ls"})

    def test_chatgpt_provider_maps_tool_history_to_responses_input(self) -> None:
        responses = CapturingResponsesCompletion(
            [{"type": "response.output_text.delta", "delta": "done"}]
        )
        client = LiteLLMInferenceClient(
            model="chatgpt/gpt-5.4-mini",
            responses_fn=responses,
        )

        async def collect():
            return [
                event
                async for event in client.stream(
                    messages=[
                        InferenceMessage(role=InferenceRole.USER, content="list files"),
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT,
                            tool_calls=[
                                ToolCall(
                                    tool_call_id="fc_1",
                                    name="shell",
                                    arguments={"command": "ls"},
                                    arguments_json="{\"command\":\"ls\"}",
                                    raw={"responses_call_id": "call-1"},
                                )
                            ],
                        ),
                        InferenceMessage(
                            role=InferenceRole.TOOL_RESULT,
                            tool_call_id="fc_1",
                            content="README.md",
                        ),
                    ],
                    tools=[],
                    config=InferenceConfig(),
                )
            ]

        anyio.run(collect)

        self.assertEqual(
            (responses.kwargs or {})["input"],
            [
                {"role": "user", "content": "list files"},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call-1",
                    "name": "shell",
                    "arguments": "{\"command\":\"ls\"}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "README.md",
                },
            ],
        )

    def test_chatgpt_default_responses_keeps_native_streaming(self) -> None:
        litellm = _import_litellm_preserving_knuth_env()

        original_aresponses = litellm.aresponses
        original_supports_native_streaming = litellm.utils.supports_native_streaming
        seen: dict[str, object] = {}

        def unsupported(model: str, custom_llm_provider: str | None = None) -> bool:
            return False

        async def fake_aresponses(**kwargs: object) -> AsyncChunks:
            seen["supports"] = litellm.utils.supports_native_streaming(
                "gpt-5.4-mini", "chatgpt"
            )
            return AsyncChunks([{"type": "response.output_text.delta", "delta": "ok"}])

        litellm.aresponses = fake_aresponses
        litellm.utils.supports_native_streaming = unsupported
        try:
            client = LiteLLMInferenceClient(model="chatgpt/gpt-5.4-mini")

            async def collect():
                return [
                    event
                    async for event in client.stream(
                        messages=[InferenceMessage(role=InferenceRole.USER, content="hi")],
                        tools=[],
                        config=InferenceConfig(),
                    )
                ]

            anyio.run(collect)

            self.assertEqual(seen["supports"], True)
            self.assertIs(litellm.utils.supports_native_streaming, unsupported)
        finally:
            litellm.aresponses = original_aresponses
            litellm.utils.supports_native_streaming = original_supports_native_streaming

    def test_chatgpt_native_streaming_patch_does_not_overlap(self) -> None:
        litellm = _import_litellm_preserving_knuth_env()

        original_aresponses = litellm.aresponses
        original_supports_native_streaming = litellm.utils.supports_native_streaming
        active = 0
        max_active = 0

        def unsupported(model: str, custom_llm_provider: str | None = None) -> bool:
            return False

        async def fake_aresponses(**kwargs: object) -> AsyncChunks:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                self.assertEqual(
                    litellm.utils.supports_native_streaming(
                        "gpt-5.4-mini", "chatgpt"
                    ),
                    True,
                )
                await anyio.sleep(0.01)
                return AsyncChunks(
                    [{"type": "response.output_text.delta", "delta": "ok"}]
                )
            finally:
                active -= 1

        litellm.aresponses = fake_aresponses
        litellm.utils.supports_native_streaming = unsupported
        try:
            client = LiteLLMInferenceClient(model="chatgpt/gpt-5.4-mini")

            async def collect_once() -> None:
                [
                    event
                    async for event in client.stream(
                        messages=[InferenceMessage(role=InferenceRole.USER, content="hi")],
                        tools=[],
                        config=InferenceConfig(),
                    )
                ]

            async def collect_two() -> None:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(collect_once)
                    tg.start_soon(collect_once)

            anyio.run(collect_two)

            self.assertEqual(max_active, 1)
            self.assertIs(litellm.utils.supports_native_streaming, unsupported)
        finally:
            litellm.aresponses = original_aresponses
            litellm.utils.supports_native_streaming = original_supports_native_streaming

    def test_chatgpt_default_responses_does_not_leak_knuth_env_from_dotenv(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            litellm = _import_litellm_preserving_knuth_env()

            original_aresponses = litellm.aresponses

            async def fake_aresponses(**kwargs: object) -> AsyncChunks:
                return AsyncChunks([{"type": "response.output_text.delta", "delta": "ok"}])

            litellm.aresponses = fake_aresponses
            try:
                client = LiteLLMInferenceClient(model="chatgpt/gpt-5.4-mini")

                async def collect() -> None:
                    [
                        event
                        async for event in client.stream(
                            messages=[InferenceMessage(role=InferenceRole.USER, content="hi")],
                            tools=[],
                            config=InferenceConfig(),
                        )
                    ]

                anyio.run(collect)
                self.assertNotIn("KNUTH_MODEL", os.environ)
                self.assertNotIn("KNUTH_API_KEY", os.environ)
                self.assertNotIn("KNUTH_BASE_URL", os.environ)
            finally:
                litellm.aresponses = original_aresponses

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
