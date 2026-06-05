from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from knuth.core.messages import (
    InferenceMessage,
    InferenceRole,
    ToolCall as CoreToolCall,
)


Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    id: str | None = None


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatResponse:
    message: ChatMessage
    tool_calls: tuple[ToolCall, ...] = ()


def chat_to_inference_message(message: ChatMessage) -> InferenceMessage:
    role = (
        InferenceRole.TOOL_RESULT
        if message.role == "tool"
        else InferenceRole(message.role)
    )
    return InferenceMessage(
        role=role,
        content=message.content,
        name=message.name,
        tool_call_id=message.tool_call_id,
        tool_name=message.name if message.role == "tool" else None,
        tool_calls=[
            CoreToolCall(
                id=call.id,
                name=call.name,
                arguments=dict(call.arguments),
                index=index,
            )
            for index, call in enumerate(message.tool_calls)
        ],
    )


def inference_to_chat_message(message: InferenceMessage) -> ChatMessage:
    role: Role = "tool" if message.role == InferenceRole.TOOL_RESULT else message.role.value  # type: ignore[assignment]
    return ChatMessage(
        role=role,
        content=message.content or "",
        name=message.tool_name or message.name,
        tool_call_id=message.tool_call_id,
        tool_calls=tuple(
            ToolCall(id=call.id, name=call.name, arguments=call.arguments)
            for call in message.tool_calls
        ),
    )
