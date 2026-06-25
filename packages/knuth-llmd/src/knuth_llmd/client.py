from __future__ import annotations

import json
import os
import warnings
from collections.abc import Callable
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence
from uuid import uuid4

import anyio
from pydantic import Field

from knuth.core.interrupts import InterruptSignal

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


class InferenceRuntimeOptions(KnuthModel):
    # An ``InterruptSignal`` (or compatible adapter). The model boundary observes
    # it cooperatively: it wakes the initial request await (TTFT) and is polled
    # between stream chunks, producing ``InferenceAborted(reason=signal.reason)``.
    abort_signal: Any | None = Field(default=None, exclude=True)


def _signal_interrupted(signal: Any) -> bool:
    if signal is None:
        return False
    interrupted = getattr(signal, "interrupted", None)
    if interrupted is not None:
        return bool(interrupted)
    is_aborted = getattr(signal, "is_aborted", None)
    return bool(is_aborted()) if is_aborted is not None else False


def _signal_reason(signal: Any) -> str:
    reason = getattr(signal, "reason", None)
    return reason if isinstance(reason, str) and reason else "user_stop"


async def _await_or_interrupt(
    signal: Any, factory: Callable[[], Any]
) -> tuple[Any, bool]:
    """Await ``factory()`` but let an interrupt wake it (model TTFT).

    Polling cannot break a single blocking await, so race it against the
    signal's wakeup. Returns ``(result, interrupted)``; on interrupt the result
    is ``None`` and the in-flight request is cancelled.
    """
    if signal is None or not hasattr(signal, "wait_interrupted"):
        return await factory(), False
    if _signal_interrupted(signal):
        return None, True

    result: list[Any] = [None]
    error: list[BaseException | None] = [None]
    interrupted = [False]

    async with anyio.create_task_group() as tg:

        async def _watch() -> None:
            await signal.wait_interrupted()
            interrupted[0] = True
            tg.cancel_scope.cancel()

        async def _work() -> None:
            try:
                result[0] = await factory()
            except Exception as exc:  # captured so the caller re-raises it
                error[0] = exc
            finally:
                tg.cancel_scope.cancel()

        tg.start_soon(_watch)
        tg.start_soon(_work)

    if error[0] is not None and not interrupted[0]:
        raise error[0]
    return result[0], interrupted[0]


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
            # raw is an accumulated view across tool-call chunks; Responses uses
            # one event for call_id and a later event for arguments.
            current["raw"] = {**current["raw"], **_to_plain(raw_call)}
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
        responses_fn: Callable[..., object] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._completion_fn = completion_fn or _default_completion_fn
        self._responses_fn = responses_fn or _default_responses_fn
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
        signal = runtime.abort_signal if runtime else None
        if _signal_interrupted(signal):
            yield event(InferenceAborted, {"reason": _signal_reason(signal)})
            return

        accumulator = StreamAccumulator()
        try:
            model = _litellm_model_name(self._model)
            use_responses = _is_chatgpt_model(model)
            if use_responses:
                kwargs = self._responses_kwargs(
                    config=config,
                    messages=messages,
                    stream=True,
                    tools=tools,
                )
                request_fn = self._responses_fn
            else:
                kwargs = self._completion_kwargs(
                    config=config,
                    messages=messages,
                    stream=True,
                    tools=tools,
                )
                request_fn = self._completion_fn
            # The initial request await (TTFT) must be wakeable by the signal,
            # not just observed after the first chunk arrives.
            response, interrupted = await _await_or_interrupt(
                signal, lambda: request_fn(**kwargs)
            )
            if interrupted:
                yield event(InferenceAborted, {"reason": _signal_reason(signal)})
                return
            async for chunk in response:  # type: ignore[attr-defined]
                if _signal_interrupted(signal):
                    yield event(InferenceAborted, {"reason": _signal_reason(signal)})
                    return
                chunks = (
                    _responses_event_to_completion_chunks(chunk)
                    if use_responses
                    else [chunk]
                )
                for normalized in chunks:
                    for event_class, fields in accumulator.feed_chunk(normalized):
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
        if config.max_output_tokens is not None and not _is_chatgpt_model(model):
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

    def _responses_kwargs(
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
                "input": _to_responses_input(messages),
                "stream": stream,
                "parallel_tool_calls": False,
            }
        )
        if _is_chatgpt_model(str(kwargs["model"])):
            kwargs["no-log"] = True
        if tools:
            kwargs["tools"] = [_to_responses_tool(tool) for tool in tools]
            kwargs["tool_choice"] = "auto"
        return kwargs


def _litellm_model_name(model: str) -> str:
    if "/" in model:
        return model
    return f"openai/{model}"


def _is_chatgpt_model(model: str) -> bool:
    return model.startswith("chatgpt/")


_CHATGPT_STREAMING_PATCH_LOCK = anyio.Lock()


def _knuth_env_snapshot() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.startswith("KNUTH_")}


def _restore_knuth_env(snapshot: Mapping[str, str]) -> None:
    for key in list(os.environ):
        if key.startswith("KNUTH_") and key not in snapshot:
            del os.environ[key]
    os.environ.update(snapshot)


def _import_litellm_preserving_knuth_env():
    snapshot = _knuth_env_snapshot()
    try:
        import litellm
    finally:
        _restore_knuth_env(snapshot)
    return litellm


def _install_litellm_response_usage_warning_filter() -> None:
    message_pattern = r"(?s)^Pydantic serializer warnings:.*ResponseAPIUsage"
    for action, message, category, _module, _lineno in warnings.filters:
        if (
            action == "ignore"
            and getattr(message, "pattern", None) == message_pattern
            and category is UserWarning
        ):
            return
    warnings.filterwarnings(
        "ignore",
        message=message_pattern,
        category=UserWarning,
    )


async def _default_completion_fn(**kwargs: object) -> object:
    litellm = _import_litellm_preserving_knuth_env()
    from litellm import acompletion

    litellm.suppress_debug_info = True  # keep "Give Feedback" banners out of the CLI
    return await acompletion(**kwargs)


async def _default_responses_fn(**kwargs: object) -> object:
    litellm = _import_litellm_preserving_knuth_env()
    from litellm import aresponses

    litellm.suppress_debug_info = True  # keep "Give Feedback" banners out of the CLI
    model = kwargs.get("model")
    if not isinstance(model, str) or not _is_chatgpt_model(model):
        return await aresponses(**kwargs)
    _install_litellm_response_usage_warning_filter()

    original_supports_native_streaming = litellm.utils.supports_native_streaming

    def supports_chatgpt_native_streaming(
        model: str, custom_llm_provider: str | None = None
    ) -> bool:
        if custom_llm_provider == "chatgpt" or _is_chatgpt_model(model):
            return True
        return original_supports_native_streaming(model, custom_llm_provider)

    async with _CHATGPT_STREAMING_PATCH_LOCK:
        litellm.utils.supports_native_streaming = supports_chatgpt_native_streaming
        try:
            return await aresponses(**kwargs)
        finally:
            litellm.utils.supports_native_streaming = original_supports_native_streaming


def _to_responses_input(messages: Sequence[InferenceMessage]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    call_ids_by_item_id: dict[str, str] = {}
    for message in messages:
        input_items.extend(_to_responses_input_items(message, call_ids_by_item_id))
    return input_items


def _to_responses_input_items(
    message: InferenceMessage, call_ids_by_item_id: dict[str, str]
) -> list[dict[str, Any]]:
    if message.role == InferenceRole.TOOL_RESULT:
        call_id = call_ids_by_item_id.get(message.tool_call_id or "")
        return [
            {
                "type": "function_call_output",
                "call_id": call_id or message.tool_call_id or "",
                "output": message.content or "",
            }
        ]
    if message.role == InferenceRole.ASSISTANT and message.tool_calls:
        items: list[dict[str, Any]] = []
        if message.content:
            items.append({"role": "assistant", "content": message.content})
        for call in message.tool_calls:
            responses_call_id = call.raw.get("responses_call_id")
            if isinstance(responses_call_id, str) and call.tool_call_id:
                call_ids_by_item_id[call.tool_call_id] = responses_call_id
        items.extend(
            {
                "type": "function_call",
                "id": call.tool_call_id,
                "call_id": call.raw.get("responses_call_id") or call.effective_id,
                "name": call.name,
                "arguments": call.arguments_as_json(),
            }
            for call in message.tool_calls
        )
        return items
    return [
        {
            "role": message.role.value,
            "content": message.content or "",
        }
    ]


def _to_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") != "function" or not isinstance(tool.get("function"), Mapping):
        return dict(tool)
    function = tool["function"]
    responses_tool: dict[str, Any] = {
        "type": "function",
        "name": function.get("name"),
    }
    if "description" in function:
        responses_tool["description"] = function["description"]
    if "parameters" in function:
        responses_tool["parameters"] = function["parameters"]
    return responses_tool


def _responses_event_to_completion_chunks(event: object) -> list[dict[str, Any]]:
    event_type = _get(event, "type")
    if event_type == "response.output_text.delta":
        delta = _get(event, "delta")
        if isinstance(delta, str) and delta:
            return [{"choices": [{"delta": {"content": delta}}]}]
        return []
    if event_type == "response.reasoning_summary_text.delta":
        delta = _get(event, "delta")
        if isinstance(delta, str) and delta:
            return [{"choices": [{"delta": {"reasoning_content": delta}}]}]
        return []
    if event_type == "response.output_item.added":
        item = _get(event, "item") or {}
        if _get(item, "type") != "function_call":
            return []
        output_index = _get(event, "output_index")
        index = output_index if isinstance(output_index, int) else 0
        item_id = _string_or_none(_get(item, "id"))
        call_id = _string_or_none(_get(item, "call_id")) or item_id
        name = _string_or_none(_get(item, "name"))
        return [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": item_id or call_id,
                                    "responses_call_id": call_id,
                                    "function": {
                                        "name": name or "",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    if event_type == "response.output_item.done":
        item = _get(event, "item") or {}
        if _get(item, "type") != "function_call":
            return []
        arguments = _string_or_none(_get(item, "arguments"))
        if not arguments:
            return []
        output_index = _get(event, "output_index")
        index = output_index if isinstance(output_index, int) else 0
        return [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {"arguments": arguments},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    if event_type == "response.function_call_arguments.delta":
        delta = _get(event, "delta")
        if not isinstance(delta, str) or not delta:
            return []
        output_index = _get(event, "output_index")
        index = output_index if isinstance(output_index, int) else 0
        return [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {
                                        "arguments": delta,
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    if event_type == "response.completed":
        return [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    if event_type in {"response.failed", "response.incomplete"}:
        error = _get(event, "error") or _get(_get(event, "response") or {}, "error")
        raise RuntimeError(str(_to_plain(error or event)))
    return []


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
