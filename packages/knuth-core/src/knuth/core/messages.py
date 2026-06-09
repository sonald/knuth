from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import Field

from knuth.core.types import KnuthModel


class InferenceRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"


class SystemSectionSource(StrEnum):
    """Closed, strongly-typed set of contributors to the system preamble."""

    BASE = "base"
    USER = "user"


class SystemSection(KnuthModel):
    """An extensible fragment composed into the ``SystemPreamble``."""

    source: SystemSectionSource
    text: str


class ToolCall(KnuthModel):
    id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_json: str | None = None
    index: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    def arguments_as_json(self) -> str:
        if self.arguments_json is not None:
            return self.arguments_json
        return json.dumps(self.arguments)


class InferenceMessage(KnuthModel):
    role: InferenceRole
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    name: str | None = None

    def to_litellm_message(self) -> dict[str, Any]:
        if self.role == InferenceRole.TOOL_RESULT:
            message: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": self.tool_call_id,
                "content": self.content or "",
            }
            if self.tool_name is not None:
                message["name"] = self.tool_name
            return message

        message = {
            "role": self.role.value,
            "content": self.content or "",
        }
        if self.name is not None:
            message["name"] = self.name
        if self.role == InferenceRole.ASSISTANT and self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_as_json(),
                    },
                }
                for call in self.tool_calls
            ]
        return message
