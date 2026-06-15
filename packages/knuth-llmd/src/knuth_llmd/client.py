from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence
from uuid import uuid4

from pydantic import Field

from knuth.core.events import (
    InferenceAborted,
    InferenceContentDelta,
    InferenceEvent,
    InferenceFailed,
    InferenceGenerationCompleted,
    InferenceGenerationStarted,
    InferenceReasoningCompleted,
    InferenceReasoningDelta,
    InferenceToolCallCompleted,
    InferenceToolCallDelta,
    InferenceToolCallStarted,
)
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth.core.types import ErrorInfo, KnuthModel


class InferenceConfig(KnuthModel):
    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_s: float | None = None
    trace_id: str | None = None
    run_id: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class AbortSignal(Protocol):
    def is_aborted(self) -> bool:
        ...

    async def checkpoint(self) -> None:
        ...


class InferenceRuntimeOptions(KnuthModel):
    abort_signal: Any | None = Field(default=None, exclude=True)


class InferenceClient(Protocol):
    @property
    def model(self) -> str:
        ...

    async def stream(
        self,
        messages: Sequence[InferenceMessage],
        tools: Sequence[dict[str, Any]],
        config: InferenceConfig,
        runtime: InferenceRuntimeOptions | None = None,
    ) -> AsyncIterator[InferenceEvent]:
        ...


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _suffix_overlap(text: str, marker: str) -> int:
    """Longest suffix of ``text`` that is a proper prefix of ``marker``."""
    max_k = min(len(text), len(marker) - 1)
    for k in range(max_k, 0, -1):
        if marker.startswith(text[-k:]):
            return k
    return 0


class _ThinkTagSplitter:
    """Split a streamed content channel into answer vs ``<think>`` reasoning.

    Some providers (e.g. MiniMax, DeepSeek-R1 via OpenAI-compatible endpoints)
    inline chain-of-thought inside the ``content`` field wrapped in
    ``<think>...</think>`` rather than emitting a separate ``reasoning_content``.
    This normalizes that into the canonical reasoning/content split, holding
    back just enough of the buffer to handle a tag split across chunks.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buf += text
        out: list[tuple[str, str]] = []
        while True:
            if not self._in_think:
                idx = self._buf.find(_THINK_OPEN)
                if idx != -1:
                    if idx > 0:
                        out.append(("content", self._buf[:idx]))
                    self._buf = self._buf[idx + len(_THINK_OPEN) :]
                    self._in_think = True
                    continue
                hold = _suffix_overlap(self._buf, _THINK_OPEN)
                if len(self._buf) > hold:
                    out.append(("content", self._buf[: len(self._buf) - hold]))
                    self._buf = self._buf[len(self._buf) - hold :]
                break
            idx = self._buf.find(_THINK_CLOSE)
            if idx != -1:
                if idx > 0:
                    out.append(("reasoning", self._buf[:idx]))
                self._buf = self._buf[idx + len(_THINK_CLOSE) :]
                self._in_think = False
                out.append(("reasoning_completed", ""))
                continue
            hold = _suffix_overlap(self._buf, _THINK_CLOSE)
            if len(self._buf) > hold:
                out.append(("reasoning", self._buf[: len(self._buf) - hold]))
                self._buf = self._buf[len(self._buf) - hold :]
            break
        return [
            (channel, chunk)
            for channel, chunk in out
            if chunk or channel == "reasoning_completed"
        ]

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        channel = "reasoning" if self._in_think else "content"
        text, self._buf = self._buf, ""
        return [(channel, text)]


class StreamAccumulator:
    def __init__(self) -> None:
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self._think = _ThinkTagSplitter()

    def feed_chunk(self, chunk: object) -> list[tuple[type, dict[str, Any]]]:
        events: list[tuple[type, dict[str, Any]]] = []
        choice = _first_choice(chunk)
        if choice is None:
            return events

        self.finish_reason = _string_or_none(_get(choice, "finish_reason")) or self.finish_reason
        delta = _get(choice, "delta")
        if delta is None:
            delta = _get(choice, "message")
        if delta is None:
            return events

        content = _get(delta, "content")
        if isinstance(content, str) and content:
            for channel, text in self._think.feed(content):
                if channel == "reasoning":
                    self.reasoning_parts.append(text)
                    events.append((InferenceReasoningDelta, {"delta": text}))
                elif channel == "reasoning_completed":
                    events.append((InferenceReasoningCompleted, {}))
                else:
                    self.content_parts.append(text)
                    events.append((InferenceContentDelta, {"delta": text}))

        reasoning = _get(delta, "reasoning_content") or _get(delta, "reasoning")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning_parts.append(reasoning)
            events.append((InferenceReasoningDelta, {"delta": reasoning}))

        for raw_call in _get(delta, "tool_calls") or ():
            index = _get(raw_call, "index")
            if not isinstance(index, int):
                index = len(self.tool_calls)
            is_new_call = index not in self.tool_calls
            current = self.tool_calls.setdefault(
                index,
                {"id": None, "name": "", "arguments_json": "", "raw": {}},
            )
            call_id = _get(raw_call, "id")
            if isinstance(call_id, str):
                current["id"] = call_id
            if is_new_call:
                events.append(
                    (
                        InferenceToolCallStarted,
                        {"index": index, "id": current["id"]},
                    )
                )
            raw_function = _get(raw_call, "function") or {}
            name = _get(raw_function, "name")
            if isinstance(name, str):
                current["name"] += name
            arguments = _get(raw_function, "arguments")
            if isinstance(arguments, str):
                current["arguments_json"] += arguments
            current["raw"] = _to_plain(raw_call)
            if isinstance(name, str) or isinstance(arguments, str):
                events.append(
                    (
                        InferenceToolCallDelta,
                        {
                            "index": index,
                            "id": current["id"],
                            "name_delta": name if isinstance(name, str) else None,
                            "arguments_json_delta": arguments
                            if isinstance(arguments, str)
                            else None,
                            "raw": current["raw"],
                        },
                    )
                )

        return events

    def finish(self) -> list[tuple[type, dict[str, Any]]]:
        events: list[tuple[type, dict[str, Any]]] = []
        for channel, text in self._think.flush():
            if channel == "reasoning":
                self.reasoning_parts.append(text)
                events.append((InferenceReasoningDelta, {"delta": text}))
            else:
                self.content_parts.append(text)
                events.append((InferenceContentDelta, {"delta": text}))
        for index in sorted(self.tool_calls):
            current = self.tool_calls[index]
            arguments_json = current["arguments_json"] or "{}"
            try:
                parsed = json.loads(arguments_json)
            except json.JSONDecodeError:
                parsed = {}
            if not isinstance(parsed, Mapping):
                parsed = {}
            call = CoreToolCall(
                tool_call_id=current["id"] or f"call_{index}",
                name=current["name"],
                arguments=dict(parsed),
                arguments_json=arguments_json,
                index=index,
                raw=current["raw"],
            )
            events.append((InferenceToolCallCompleted, {"tool_call": call}))
        return events

    def to_message(self) -> InferenceMessage:
        calls: list[CoreToolCall] = []
        for index in sorted(self.tool_calls):
            current = self.tool_calls[index]
            arguments_json = current["arguments_json"] or "{}"
            try:
                parsed = json.loads(arguments_json)
            except json.JSONDecodeError:
                parsed = {}
            if not isinstance(parsed, Mapping):
                parsed = {}
            calls.append(
                CoreToolCall(
                    tool_call_id=current["id"] or f"call_{index}",
                    name=current["name"],
                    arguments=dict(parsed),
                    arguments_json=arguments_json,
                    index=index,
                    raw=current["raw"],
                )
            )
        return InferenceMessage(
            role=InferenceRole.ASSISTANT,
            content="".join(self.content_parts),
            tool_calls=calls,
        )


class LiteLLMInferenceClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        completion_fn: Callable[..., object] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._completion_fn = completion_fn or _default_completion_fn
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self,
        messages: Sequence[InferenceMessage],
        tools: Sequence[dict[str, Any]],
        config: InferenceConfig,
        runtime: InferenceRuntimeOptions | None = None,
    ) -> AsyncIterator[InferenceEvent]:
        generation_id = f"gen_{uuid4().hex}"
        seq = 0

        def event(
            event_class: type, fields: Mapping[str, object] | None = None
        ) -> InferenceEvent:
            nonlocal seq
            seq += 1
            return event_class(
                generation_id=generation_id,
                seq=seq,
                run_id=config.run_id,
                **(fields or {}),
            )

        yield event(InferenceGenerationStarted, {"model": self.model})
        if runtime and runtime.abort_signal:
            await runtime.abort_signal.checkpoint()

        accumulator = StreamAccumulator()
        try:
            kwargs = self._completion_kwargs(
                config=config,
                messages=messages,
                stream=True,
                tools=tools,
            )
            response = await self._completion_fn(**kwargs)
            async for chunk in response:  # type: ignore[attr-defined]
                if runtime and runtime.abort_signal and runtime.abort_signal.is_aborted():
                    yield event(InferenceAborted, {"reason": "abort_signal"})
                    return
                for event_class, fields in accumulator.feed_chunk(chunk):
                    yield event(event_class, fields)
            for event_class, fields in accumulator.finish():
                yield event(event_class, fields)
            yield event(
                InferenceGenerationCompleted,
                {
                    "finish_reason": accumulator.finish_reason,
                    "message": accumulator.to_message(),
                },
            )
        except Exception as exc:
            yield event(
                InferenceFailed,
                {
                    "error": ErrorInfo(
                        code=exc.__class__.__name__,
                        message=str(exc),
                        retryable=False,
                    )
                },
            )

    def _base_kwargs(self, config: InferenceConfig) -> dict[str, object]:
        model = _litellm_model_name(self._model)
        kwargs: dict[str, object] = {
            "model": model,
            "timeout": config.timeout_s or self._timeout,
        }
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.max_output_tokens is not None:
            kwargs["max_tokens"] = config.max_output_tokens
        kwargs.update(config.provider_options)
        return kwargs

    def _completion_kwargs(
        self,
        *,
        config: InferenceConfig,
        messages: Sequence[InferenceMessage],
        stream: bool,
        tools: Sequence[dict[str, Any]],
    ) -> dict[str, object]:
        kwargs = self._base_kwargs(config)
        kwargs.update(
            {
                "messages": [message.to_litellm_message() for message in messages],
                "stream": stream,
                "parallel_tool_calls": False,
            }
        )
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"
        return kwargs


def _litellm_model_name(model: str) -> str:
    if "/" in model:
        return model
    return f"openai/{model}"


async def _default_completion_fn(**kwargs: object) -> object:
    import litellm
    from litellm import acompletion

    litellm.suppress_debug_info = True  # keep "Give Feedback" banners out of the CLI
    return await acompletion(**kwargs)


def _get(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _first_choice(response: object) -> object | None:
    choices = _get(response, "choices")
    if isinstance(choices, Sequence) and not isinstance(choices, str) and choices:
        return choices[0]
    return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _to_plain(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return {k: _to_plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value
