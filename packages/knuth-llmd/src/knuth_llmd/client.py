from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence
from uuid import uuid4

from pydantic import Field

from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth.core.types import ErrorInfo, KnuthModel
from knuth_llmd.types import ToolSpec


class InferenceEventType(StrEnum):
    GENERATION_START = "generation_start"
    GENERATION_END = "generation_end"
    CONTENT_DELTA = "content_delta"
    CONTENT = "content"
    REASONING_DELTA = "reasoning_delta"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    ERROR = "error"
    ABORTED = "aborted"


class UsageInfo(KnuthModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


class InferenceConfig(KnuthModel):
    model: str
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


class InferenceEvent(KnuthModel):
    type: InferenceEventType
    generation_id: str
    seq: int
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class InferenceResult(KnuthModel):
    message: InferenceMessage
    finish_reason: str | None = None
    usage: UsageInfo | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class InferenceClient(ABC):
    @abstractmethod
    async def stream(
        self,
        messages: Sequence[InferenceMessage],
        tools: Sequence[dict[str, Any]],
        config: InferenceConfig,
        runtime: InferenceRuntimeOptions | None = None,
    ) -> AsyncIterator[InferenceEvent]:
        ...

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[InferenceMessage],
        config: InferenceConfig,
        tools: Sequence[dict[str, Any]] = (),
        runtime: InferenceRuntimeOptions | None = None,
    ) -> InferenceResult:
        ...


class StreamAccumulator:
    def __init__(self) -> None:
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None

    def feed_chunk(self, chunk: object) -> list[tuple[InferenceEventType, dict[str, Any]]]:
        events: list[tuple[InferenceEventType, dict[str, Any]]] = []
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
            self.content_parts.append(content)
            events.append((InferenceEventType.CONTENT_DELTA, {"delta": content}))

        reasoning = _get(delta, "reasoning_content") or _get(delta, "reasoning")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning_parts.append(reasoning)
            events.append((InferenceEventType.REASONING_DELTA, {"delta": reasoning}))

        for raw_call in _get(delta, "tool_calls") or ():
            index = _get(raw_call, "index")
            if not isinstance(index, int):
                index = len(self.tool_calls)
            current = self.tool_calls.setdefault(
                index,
                {"id": None, "name": "", "arguments_json": "", "raw": {}},
            )
            call_id = _get(raw_call, "id")
            if isinstance(call_id, str):
                current["id"] = call_id
            raw_function = _get(raw_call, "function") or {}
            name = _get(raw_function, "name")
            if isinstance(name, str):
                current["name"] += name
            arguments = _get(raw_function, "arguments")
            if isinstance(arguments, str):
                current["arguments_json"] += arguments
            current["raw"] = _to_plain(raw_call)

        return events

    def finish(self) -> list[tuple[InferenceEventType, dict[str, Any]]]:
        events: list[tuple[InferenceEventType, dict[str, Any]]] = []
        content = "".join(self.content_parts)
        reasoning = "".join(self.reasoning_parts)
        if content:
            events.append((InferenceEventType.CONTENT, {"content": content}))
        if reasoning:
            events.append((InferenceEventType.REASONING, {"reasoning": reasoning}))
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
                id=current["id"] or f"call_{index}",
                name=current["name"],
                arguments=dict(parsed),
                arguments_json=arguments_json,
                index=index,
                raw=current["raw"],
            )
            events.append((InferenceEventType.TOOL_CALL, {"tool_call": call.model_dump()}))
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
                    id=current["id"] or f"call_{index}",
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


class LiteLLMInferenceClient(InferenceClient):
    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        completion_fn: Callable[..., object] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._model = _litellm_model_name(model)
        self._base_url = base_url
        self._api_key = api_key
        self._completion_fn = completion_fn or _default_completion_fn
        self._timeout = timeout

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
            event_type: InferenceEventType, payload: dict[str, Any] | None = None
        ) -> InferenceEvent:
            nonlocal seq
            seq += 1
            return InferenceEvent(
                type=event_type,
                generation_id=generation_id,
                seq=seq,
                run_id=config.run_id,
                payload=payload or {},
            )

        yield event(InferenceEventType.GENERATION_START, {"model": config.model})
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
                    yield event(InferenceEventType.ABORTED, {"reason": "abort_signal"})
                    return
                for event_type, payload in accumulator.feed_chunk(chunk):
                    yield event(event_type, payload)
            for event_type, payload in accumulator.finish():
                yield event(event_type, payload)
            yield event(
                InferenceEventType.GENERATION_END,
                {
                    "finish_reason": accumulator.finish_reason,
                    "message": accumulator.to_message().model_dump(),
                },
            )
        except Exception as exc:
            yield event(
                InferenceEventType.ERROR,
                ErrorInfo(
                    code=exc.__class__.__name__,
                    message=str(exc),
                    retryable=False,
                ).model_dump(),
            )

    async def complete(
        self,
        messages: Sequence[InferenceMessage],
        config: InferenceConfig,
        tools: Sequence[dict[str, Any]] = (),
        runtime: InferenceRuntimeOptions | None = None,
    ) -> InferenceResult:
        if runtime and runtime.abort_signal:
            await runtime.abort_signal.checkpoint()
        response = await self._completion_fn(
            **self._completion_kwargs(
                config=config,
                messages=messages,
                stream=False,
                tools=tools,
            )
        )
        return InferenceResult(
            message=_parse_inference_message(response),
            finish_reason=_parse_finish_reason(response),
            raw=_to_plain(response),
        )

    def _base_kwargs(self, config: InferenceConfig) -> dict[str, object]:
        model = _litellm_model_name(config.model or self._model)
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
    from litellm import acompletion

    return await acompletion(**kwargs)


def tool_spec_to_payload(tool: ToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


def _parse_inference_message(response: object) -> InferenceMessage:
    choices = _get(response, "choices")
    if not isinstance(choices, Sequence) or isinstance(choices, str) or not choices:
        raise RuntimeError("LLM response did not include choices")

    first_choice = choices[0]
    raw_message = _get(first_choice, "message")
    if raw_message is None:
        raise RuntimeError("LLM response choice did not include a message")

    content = _get(raw_message, "content") or ""
    raw_tool_calls = _get(raw_message, "tool_calls") or ()
    return InferenceMessage(
        role=InferenceRole.ASSISTANT,
        content=str(content),
        tool_calls=[
            _parse_tool_call(item, index)
            for index, item in enumerate(raw_tool_calls)
        ],
    )


def _parse_finish_reason(response: object) -> str | None:
    choice = _first_choice(response)
    if choice is None:
        return None
    return _string_or_none(_get(choice, "finish_reason"))


def _parse_tool_call(raw_call: object, fallback_index: int) -> CoreToolCall:
    raw_function = _get(raw_call, "function")
    if raw_function is None:
        raise RuntimeError("LLM tool call did not include a function")

    name = _get(raw_function, "name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("LLM tool call did not include a function name")

    arguments = _get(raw_function, "arguments") or "{}"
    if not isinstance(arguments, str):
        raise RuntimeError("LLM tool call arguments were not a JSON string")
    parsed_arguments = json.loads(arguments)
    if not isinstance(parsed_arguments, Mapping):
        raise RuntimeError("LLM tool call arguments were not an object")

    call_id = _get(raw_call, "id")
    index = _get(raw_call, "index")
    return CoreToolCall(
        id=call_id if isinstance(call_id, str) else None,
        name=name,
        arguments=dict(parsed_arguments),
        arguments_json=arguments,
        index=index if isinstance(index, int) else fallback_index,
        raw=_to_plain(raw_call),
    )


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
