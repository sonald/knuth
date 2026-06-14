"""AG-UI protocol facade used by the Knuth transport.

The package keeps event construction behind this small module so the rest of
``knuth-agui`` does not need to know the SDK's concrete class names. Encoding is
delegated to the official ``ag-ui-protocol`` encoder.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeAlias

from ag_ui.core import (
    AssistantMessage,
    CustomEvent,
    Event,
    FunctionCall,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    SystemMessage,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    ThinkingTextMessageStartEvent,
    ToolCall,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
    ToolMessage,
    UserMessage,
)
from ag_ui.encoder import EventEncoder
from knuth.core.messages import InferenceMessage, InferenceRole

AGUIEvent: TypeAlias = Event

_ENCODER = EventEncoder()


def content_type() -> str:
    return _ENCODER.get_content_type()


def run_started(thread_id: str, run_id: str) -> AGUIEvent:
    return RunStartedEvent(threadId=thread_id, runId=run_id)


def run_finished(thread_id: str, run_id: str) -> AGUIEvent:
    return RunFinishedEvent(threadId=thread_id, runId=run_id)


def run_error(message: str, code: str | None = None) -> AGUIEvent:
    return RunErrorEvent(message=message, code=code)


def text_message_start(message_id: str, role: str = "assistant") -> AGUIEvent:
    return TextMessageStartEvent(messageId=message_id, role=role)


def text_message_content(message_id: str, delta: str) -> AGUIEvent:
    return TextMessageContentEvent(messageId=message_id, delta=delta)


def text_message_end(message_id: str) -> AGUIEvent:
    return TextMessageEndEvent(messageId=message_id)


def thinking_start() -> AGUIEvent:
    return ThinkingStartEvent()


def thinking_text_start() -> AGUIEvent:
    return ThinkingTextMessageStartEvent()


def thinking_text_content(delta: str) -> AGUIEvent:
    return ThinkingTextMessageContentEvent(delta=delta)


def thinking_text_end() -> AGUIEvent:
    return ThinkingTextMessageEndEvent()


def thinking_end() -> AGUIEvent:
    return ThinkingEndEvent()


def tool_call_start(
    tool_call_id: str, tool_call_name: str, parent_message_id: str | None = None
) -> AGUIEvent:
    return ToolCallStartEvent(
        toolCallId=tool_call_id,
        toolCallName=tool_call_name,
        parentMessageId=parent_message_id,
    )


def tool_call_args(tool_call_id: str, delta: str) -> AGUIEvent:
    return ToolCallArgsEvent(toolCallId=tool_call_id, delta=delta)


def tool_call_end(tool_call_id: str) -> AGUIEvent:
    return ToolCallEndEvent(toolCallId=tool_call_id)


def tool_call_result(
    message_id: str, tool_call_id: str, content: str, role: str = "tool"
) -> AGUIEvent:
    return ToolCallResultEvent(
        messageId=message_id,
        toolCallId=tool_call_id,
        content=content,
        role=role,
    )


def custom(name: str, value: Any) -> AGUIEvent:
    return CustomEvent(name=name, value=value)


def messages_snapshot(messages: Sequence[InferenceMessage]) -> AGUIEvent:
    return MessagesSnapshotEvent(
        messages=[
            _to_agui_message(message, index)
            for index, message in enumerate(messages, start=1)
        ]
    )


def _to_agui_message(message: InferenceMessage, index: int):
    message_id = f"msg_history_{index}"
    content = message.content or ""
    if message.role == InferenceRole.SYSTEM:
        return SystemMessage(id=message_id, content=content, name=message.name)
    if message.role == InferenceRole.USER:
        return UserMessage(id=message_id, content=content, name=message.name)
    if message.role == InferenceRole.TOOL_RESULT:
        return ToolMessage(
            id=message_id,
            content=content,
            toolCallId=message.tool_call_id or "",
        )
    tool_calls = [
        ToolCall(
            id=call.effective_id,
            function=FunctionCall(name=call.name, arguments=call.arguments_as_json()),
        )
        for call in message.tool_calls
    ]
    return AssistantMessage(
        id=message_id,
        content=message.content,
        name=message.name,
        toolCalls=tool_calls or None,
    )


def encode_sse(event: AGUIEvent) -> str:
    return _ENCODER.encode(event)
