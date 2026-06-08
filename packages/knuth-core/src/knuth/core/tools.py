from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from knuth.core.messages import ToolCall
from knuth.core.types import ErrorInfo, KnuthModel


class ToolProposalStatus(StrEnum):
    ALLOWED = "allowed"
    REQUIRES_APPROVAL = "requires_approval"
    DENIED = "denied"


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ApprovalRequest(KnuthModel):
    id: str
    run_id: str
    title: str
    reason: str
    risk: str
    payload: dict[str, Any]


class ToolIntent(KnuthModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    index: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_tool_call(cls, call: ToolCall) -> "ToolIntent":
        return cls(
            id=call.id or f"call_{call.index}",
            name=call.name,
            arguments=call.arguments,
            index=call.index,
            raw=call.raw,
        )


class ToolProposal(KnuthModel):
    status: ToolProposalStatus
    intent: ToolIntent
    normalized_args: dict[str, Any] = Field(default_factory=dict)
    approval: ApprovalRequest | None = None
    error: ErrorInfo | None = None


class ToolResult(KnuthModel):
    status: ToolResultStatus
    content: str | None = None
    data: Any = None
    error: ErrorInfo | None = None
    artifacts: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS

    def to_observation_text(self) -> str:
        if self.status == ToolResultStatus.SUCCESS:
            return self.content if self.content is not None else repr(self.data)
        return f"Tool error: {self.error.message if self.error else 'unknown error'}"

    @classmethod
    def success(cls, content: str | None = None, data: Any = None) -> "ToolResult":
        return cls(status=ToolResultStatus.SUCCESS, content=content, data=data)

    @classmethod
    def from_error(
        cls, code: str, message: str, retryable: bool = False
    ) -> "ToolResult":
        return cls(
            status=ToolResultStatus.ERROR,
            content="",
            error=ErrorInfo(code=code, message=message, retryable=retryable),
        )
