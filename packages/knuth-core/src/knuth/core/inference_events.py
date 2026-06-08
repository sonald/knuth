from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from knuth.core.messages import InferenceMessage, ToolCall
from knuth.core.types import ErrorInfo, KnuthModel


class UsageInfo(KnuthModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


class InferenceEventBase(KnuthModel):
    type: str
    generation_id: str
    seq: int
    run_id: str | None = None


class InferenceGenerationStarted(InferenceEventBase):
    type: Literal["inference.generation.started"] = "inference.generation.started"
    model: str


class InferenceReasoningDelta(InferenceEventBase):
    type: Literal["inference.reasoning.delta"] = "inference.reasoning.delta"
    delta: str


class InferenceReasoningCompleted(InferenceEventBase):
    type: Literal["inference.reasoning.completed"] = "inference.reasoning.completed"


class InferenceContentDelta(InferenceEventBase):
    type: Literal["inference.content.delta"] = "inference.content.delta"
    delta: str


class InferenceToolCallStarted(InferenceEventBase):
    type: Literal["inference.tool_call.started"] = "inference.tool_call.started"
    index: int
    id: str | None = None


class InferenceToolCallDelta(InferenceEventBase):
    type: Literal["inference.tool_call.delta"] = "inference.tool_call.delta"
    index: int
    id: str | None = None
    name_delta: str | None = None
    arguments_json_delta: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class InferenceToolCallCompleted(InferenceEventBase):
    type: Literal["inference.tool_call.completed"] = "inference.tool_call.completed"
    tool_call: ToolCall


class InferenceGenerationCompleted(InferenceEventBase):
    type: Literal["inference.generation.completed"] = "inference.generation.completed"
    message: InferenceMessage
    finish_reason: str | None = None
    usage: UsageInfo | None = None


class InferenceFailed(InferenceEventBase):
    type: Literal["inference.failed"] = "inference.failed"
    error: ErrorInfo


class InferenceAborted(InferenceEventBase):
    type: Literal["inference.aborted"] = "inference.aborted"
    reason: str


InferenceEvent = (
    InferenceGenerationStarted
    | InferenceReasoningDelta
    | InferenceReasoningCompleted
    | InferenceContentDelta
    | InferenceToolCallStarted
    | InferenceToolCallDelta
    | InferenceToolCallCompleted
    | InferenceGenerationCompleted
    | InferenceFailed
    | InferenceAborted
)
