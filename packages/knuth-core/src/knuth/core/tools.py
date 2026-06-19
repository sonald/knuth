from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from knuth.core.types import ErrorInfo, KnuthModel


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ToolResult(KnuthModel):
    status: ToolResultStatus
    content: str | None = None
    data: Any = None
    error: ErrorInfo | None = None
    artifacts: list[str] = Field(default_factory=list)
    condensed: bool = False

    @property
    def ok(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS

    def to_observation_text(self) -> str:
        if self.content is not None:
            return self.content
        if self.status == ToolResultStatus.SUCCESS:
            return repr(self.data)
        return f"Tool error: {self.error.message if self.error else 'unknown error'}"

    @classmethod
    def success(
        cls,
        content: str | None = None,
        data: object = None,
        *,
        artifacts: list[str] | None = None,
        condensed: bool = False,
    ) -> "ToolResult":
        return cls(
            status=ToolResultStatus.SUCCESS,
            content=content,
            data=data,
            artifacts=list(artifacts or []),
            condensed=condensed,
        )

    @classmethod
    def from_error(
        cls, code: str, message: str, retryable: bool = False
    ) -> "ToolResult":
        return cls(
            status=ToolResultStatus.ERROR,
            content=None,
            error=ErrorInfo(code=code, message=message, retryable=retryable),
        )


class ToolExecutionOutcome(StrEnum):
    """How a tool execution attempt resolved at the runtime execution layer.

    Distinct from :class:`ToolResultStatus`: ``UNKNOWN`` is not a tool result and
    must never become a tool_result message — it is the indeterminate-side-effect
    case routed to recovery. ``INTERRUPTED`` is a cooperative stop reported by the
    tool/provider, carrying a model-visible observation.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class ToolExecutionResult(KnuthModel):
    """A tool/provider's cooperative report of one execution attempt.

    Tools may return a plain :class:`ToolResult` (normalized to succeeded/failed)
    or this richer report when they need to express ``interrupted``/``unknown``
    with a custom observation, e.g. a shell tool warning about partial side
    effects after a user stop.
    """

    outcome: ToolExecutionOutcome
    result: ToolResult | None = None
    observation: str | None = None
    reason: str | None = None
    tool_status: str | None = None

    def to_observation_text(self) -> str:
        if self.observation is not None:
            return self.observation
        if self.result is not None:
            return self.result.to_observation_text()
        return ""

    @classmethod
    def succeeded(cls, result: ToolResult) -> "ToolExecutionResult":
        return cls(outcome=ToolExecutionOutcome.SUCCEEDED, result=result)

    @classmethod
    def failed(cls, result: ToolResult) -> "ToolExecutionResult":
        return cls(outcome=ToolExecutionOutcome.FAILED, result=result)

    @classmethod
    def interrupted(
        cls, observation: str, *, tool_status: str | None = None
    ) -> "ToolExecutionResult":
        return cls(
            outcome=ToolExecutionOutcome.INTERRUPTED,
            observation=observation,
            tool_status=tool_status or "interrupted",
        )

    @classmethod
    def unknown(cls, reason: str) -> "ToolExecutionResult":
        return cls(outcome=ToolExecutionOutcome.UNKNOWN, reason=reason)
