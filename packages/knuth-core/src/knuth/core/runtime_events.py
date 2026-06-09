from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from knuth.core.inference_events import UsageInfo
from knuth.core.messages import InferenceMessage, ToolCall
from knuth.core.tools import ToolIntent, ToolProposal, ToolResult
from knuth.core.types import ErrorInfo, EventDurability, KnuthModel


class RuntimeEventDraftBase(KnuthModel):
    type: str
    durability: EventDurability = EventDurability.DURABLE


class RunCreatedDraft(RuntimeEventDraftBase):
    type: Literal["run.created"] = "run.created"
    query: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserMessageDraft(RuntimeEventDraftBase):
    type: Literal["user.message"] = "user.message"
    content: str


class ModelStartedDraft(RuntimeEventDraftBase):
    type: Literal["model.started"] = "model.started"
    turn: int
    model: str
    message_count: int
    tool_count: int


class ModelCompletedDraft(RuntimeEventDraftBase):
    type: Literal["model.completed"] = "model.completed"
    turn: int
    message: InferenceMessage
    finish_reason: str | None = None
    usage: UsageInfo | None = None


class ModelAbortedDraft(RuntimeEventDraftBase):
    type: Literal["model.aborted"] = "model.aborted"
    reason: str


class ModelFailedDraft(RuntimeEventDraftBase):
    type: Literal["model.failed"] = "model.failed"
    error: ErrorInfo


class ToolIntentDraft(RuntimeEventDraftBase):
    type: Literal["tool.intent"] = "tool.intent"
    intent: ToolIntent


class ToolProposedDraft(RuntimeEventDraftBase):
    type: Literal["tool.proposed"] = "tool.proposed"
    proposal: ToolProposal


class ToolStartedDraft(RuntimeEventDraftBase):
    type: Literal["tool.started"] = "tool.started"
    intent: ToolIntent


class ToolCompletedDraft(RuntimeEventDraftBase):
    type: Literal["tool.completed"] = "tool.completed"
    intent: ToolIntent
    message: InferenceMessage
    outcome: Literal["succeeded", "failed", "denied"]
    result: ToolResult | None = None


class ApprovalRequestedDraft(RuntimeEventDraftBase):
    type: Literal["approval.requested"] = "approval.requested"
    approval_id: str
    tool_call_id: str | None = None
    title: str
    reason: str
    risk: str | None = None


class RunSucceededDraft(RuntimeEventDraftBase):
    type: Literal["run.succeeded"] = "run.succeeded"
    answer: str
    turns: int


class RunFailedDraft(RuntimeEventDraftBase):
    type: Literal["run.failed"] = "run.failed"
    reason: str
    max_turns: int | None = None


class VerificationFailedDraft(RuntimeEventDraftBase):
    type: Literal["verification.failed"] = "verification.failed"
    ok: Literal[False] = False
    reason: str


DurableRuntimeEventDraft = (
    RunCreatedDraft
    | UserMessageDraft
    | ModelStartedDraft
    | ModelCompletedDraft
    | ModelAbortedDraft
    | ModelFailedDraft
    | ToolIntentDraft
    | ToolProposedDraft
    | ToolStartedDraft
    | ToolCompletedDraft
    | ApprovalRequestedDraft
    | RunSucceededDraft
    | RunFailedDraft
    | VerificationFailedDraft
)


class TransientRuntimeEventDraftBase(RuntimeEventDraftBase):
    durability: EventDurability = EventDurability.TRANSIENT


class ModelReasoningDeltaDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.reasoning.delta"] = "model.reasoning.delta"
    delta: str


class ModelReasoningCompletedDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.reasoning.completed"] = "model.reasoning.completed"


class ModelContentDeltaDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.content.delta"] = "model.content.delta"
    delta: str


class ModelToolCallStartedDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.tool_call.started"] = "model.tool_call.started"
    index: int
    id: str | None = None


class ModelToolCallDeltaDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.tool_call.delta"] = "model.tool_call.delta"
    index: int
    id: str | None = None
    name_delta: str | None = None
    arguments_json_delta: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelToolCallCompletedDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.tool_call.completed"] = "model.tool_call.completed"
    tool_call: ToolCall


TransientRuntimeEventDraft = (
    ModelReasoningDeltaDraft
    | ModelReasoningCompletedDraft
    | ModelContentDeltaDraft
    | ModelToolCallStartedDraft
    | ModelToolCallDeltaDraft
    | ModelToolCallCompletedDraft
)

RuntimeEventDraft = DurableRuntimeEventDraft | TransientRuntimeEventDraft


class StoredRuntimeEventBase(KnuthModel):
    id: str
    run_id: str
    seq: int
    type: str
    durability: EventDurability = EventDurability.DURABLE
    created_at: str


class TransientRuntimeEventBase(KnuthModel):
    id: str
    run_id: str
    type: str
    durability: EventDurability = EventDurability.TRANSIENT
    created_at: str


def _stored(name: str, draft: type[RuntimeEventDraftBase]):
    return type(name, (StoredRuntimeEventBase, draft), {})


RunCreated = _stored("RunCreated", RunCreatedDraft)
UserMessage = _stored("UserMessage", UserMessageDraft)
ModelStarted = _stored("ModelStarted", ModelStartedDraft)
ModelCompleted = _stored("ModelCompleted", ModelCompletedDraft)
ModelAborted = _stored("ModelAborted", ModelAbortedDraft)
ModelFailed = _stored("ModelFailed", ModelFailedDraft)
ToolIntentEvent = _stored("ToolIntentEvent", ToolIntentDraft)
ToolProposed = _stored("ToolProposed", ToolProposedDraft)
ToolStarted = _stored("ToolStarted", ToolStartedDraft)
ToolCompleted = _stored("ToolCompleted", ToolCompletedDraft)
ApprovalRequested = _stored("ApprovalRequested", ApprovalRequestedDraft)
RunSucceeded = _stored("RunSucceeded", RunSucceededDraft)
RunFailed = _stored("RunFailed", RunFailedDraft)
VerificationFailed = _stored("VerificationFailed", VerificationFailedDraft)


def _transient(name: str, draft: type[TransientRuntimeEventDraftBase]):
    return type(name, (TransientRuntimeEventBase, draft), {})


ModelReasoningDelta = _transient("ModelReasoningDelta", ModelReasoningDeltaDraft)
ModelReasoningCompleted = _transient(
    "ModelReasoningCompleted", ModelReasoningCompletedDraft
)
ModelContentDelta = _transient("ModelContentDelta", ModelContentDeltaDraft)
ModelToolCallStarted = _transient("ModelToolCallStarted", ModelToolCallStartedDraft)
ModelToolCallDelta = _transient("ModelToolCallDelta", ModelToolCallDeltaDraft)
ModelToolCallCompleted = _transient(
    "ModelToolCallCompleted", ModelToolCallCompletedDraft
)


StoredRuntimeEvent = (
    RunCreated
    | UserMessage
    | ModelStarted
    | ModelCompleted
    | ModelAborted
    | ModelFailed
    | ToolIntentEvent
    | ToolProposed
    | ToolStarted
    | ToolCompleted
    | ApprovalRequested
    | RunSucceeded
    | RunFailed
    | VerificationFailed
)

TransientRuntimeEvent = (
    ModelReasoningDelta
    | ModelReasoningCompleted
    | ModelContentDelta
    | ModelToolCallStarted
    | ModelToolCallDelta
    | ModelToolCallCompleted
)

RuntimeEvent = StoredRuntimeEvent | TransientRuntimeEvent
