from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from knuth.core.inference_events import UsageInfo
from knuth.core.invocations import ToolCallDecision, ToolEffect, ToolRisk
from knuth.core.messages import ToolCall
from knuth.core.types import ErrorInfo, EventDurability, KnuthModel, RunStatus


class ContextSnapshot(KnuthModel):
    """Hash-level proof of what one model call saw, frozen at build time."""

    messages_hash: str
    tools_hash: str
    preamble_hash: str
    model_config_hash: str
    message_count: int
    tool_count: int


class PlannedToolCall(KnuthModel):
    """One frozen tool call inside a ``tool.batch_planned`` event."""

    tool_call_id: str
    index: int = 0
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    args_hash: str


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


class RunResumedDraft(RuntimeEventDraftBase):
    type: Literal["run.resumed"] = "run.resumed"
    cause: Literal["approval_resolved", "user_message", "user_resume"]


class RunPausedDraft(RuntimeEventDraftBase):
    type: Literal["run.paused"] = "run.paused"
    reason: str
    source: Literal["control", "hook"] = "control"


class RunCancelledDraft(RuntimeEventDraftBase):
    type: Literal["run.cancelled"] = "run.cancelled"
    reason: str
    source: Literal["control", "hook"] = "control"


class RunFailedDraft(RuntimeEventDraftBase):
    type: Literal["run.failed"] = "run.failed"
    error: ErrorInfo


class RunSucceededDraft(RuntimeEventDraftBase):
    type: Literal["run.succeeded"] = "run.succeeded"
    answer: str
    turns: int


class StepStartedDraft(RuntimeEventDraftBase):
    type: Literal["step.started"] = "step.started"
    step_id: str
    index: int
    snapshot: ContextSnapshot


class ModelCompletedDraft(RuntimeEventDraftBase):
    type: Literal["model.completed"] = "model.completed"
    step_id: str
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: UsageInfo | None = None


class ModelFailedDraft(RuntimeEventDraftBase):
    type: Literal["model.failed"] = "model.failed"
    step_id: str | None = None
    error: ErrorInfo


class ModelAbortedDraft(RuntimeEventDraftBase):
    type: Literal["model.aborted"] = "model.aborted"
    step_id: str | None = None
    reason: str


class ToolBatchPlannedDraft(RuntimeEventDraftBase):
    type: Literal["tool.batch_planned"] = "tool.batch_planned"
    batch_id: str
    step_id: str
    calls: list[PlannedToolCall]


class ToolProposedDraft(RuntimeEventDraftBase):
    type: Literal["tool.proposed"] = "tool.proposed"
    tool_call_id: str
    decision: ToolCallDecision
    effect: ToolEffect = ToolEffect.READ
    risk: ToolRisk = ToolRisk.LOW
    error: ErrorInfo | None = None


class ApprovalRequestedDraft(RuntimeEventDraftBase):
    type: Literal["approval.requested"] = "approval.requested"
    approval_id: str
    tool_call_id: str
    args_hash: str
    title: str
    reason: str
    risk: str
    preview: dict[str, Any] = Field(default_factory=dict)


class ApprovalResolvedDraft(RuntimeEventDraftBase):
    type: Literal["approval.resolved"] = "approval.resolved"
    approval_id: str
    resolution: Literal["approved", "denied"]
    resolved_by: str | None = None


class ToolInvocationStartedDraft(RuntimeEventDraftBase):
    type: Literal["tool.invocation_started"] = "tool.invocation_started"
    tool_call_id: str
    idempotency_key: str
    attempt: int = 1


class ToolInvocationCompletedDraft(RuntimeEventDraftBase):
    type: Literal["tool.invocation_completed"] = "tool.invocation_completed"
    tool_call_id: str
    tool_name: str
    outcome: Literal["succeeded", "failed", "denied"]
    observation: str | None = None
    observation_ref: str | None = None
    observation_preview: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationMarkedUnknownDraft(RuntimeEventDraftBase):
    type: Literal["tool.invocation_marked_unknown"] = "tool.invocation_marked_unknown"
    tool_call_id: str
    reason: str


class ToolBatchClosedDraft(RuntimeEventDraftBase):
    type: Literal["tool.batch_closed"] = "tool.batch_closed"
    batch_id: str


class VerificationFailedDraft(RuntimeEventDraftBase):
    type: Literal["verification.failed"] = "verification.failed"
    reason: str
    feedback: str


class ContextCompactedDraft(RuntimeEventDraftBase):
    """Reserved: history compaction as an appended fact. Not implemented in v0."""

    type: Literal["context.compacted"] = "context.compacted"
    replaces_through_seq: int
    summary: str | None = None
    summary_ref: str | None = None


class RunCheckpointDraft(RuntimeEventDraftBase):
    """Reserved: fold anchor. Not implemented in v0."""

    type: Literal["run.checkpoint"] = "run.checkpoint"
    through_seq: int
    state_ref: str | None = None


DurableRuntimeEventDraft = (
    RunCreatedDraft
    | UserMessageDraft
    | RunResumedDraft
    | RunPausedDraft
    | RunCancelledDraft
    | RunFailedDraft
    | RunSucceededDraft
    | StepStartedDraft
    | ModelCompletedDraft
    | ModelFailedDraft
    | ModelAbortedDraft
    | ToolBatchPlannedDraft
    | ToolProposedDraft
    | ApprovalRequestedDraft
    | ApprovalResolvedDraft
    | ToolInvocationStartedDraft
    | ToolInvocationCompletedDraft
    | ToolInvocationMarkedUnknownDraft
    | ToolBatchClosedDraft
    | VerificationFailedDraft
    | ContextCompactedDraft
    | RunCheckpointDraft
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
    tool_call_id: str | None = None


class ModelToolCallDeltaDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.tool_call.delta"] = "model.tool_call.delta"
    index: int
    tool_call_id: str | None = None
    name_delta: str | None = None
    arguments_json_delta: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelToolCallCompletedDraft(TransientRuntimeEventDraftBase):
    type: Literal["model.tool_call.completed"] = "model.tool_call.completed"
    tool_call: ToolCall


class RunInvocationStartedDraft(TransientRuntimeEventDraftBase):
    type: Literal["run.invocation.started"] = "run.invocation.started"
    mode: Literal["start", "continue", "resume"]


class RunInvocationEndedDraft(TransientRuntimeEventDraftBase):
    type: Literal["run.invocation.ended"] = "run.invocation.ended"
    mode: Literal["start", "continue", "resume"]
    status: RunStatus | None = None
    error: ErrorInfo | None = None


TransientRuntimeEventDraft = (
    ModelReasoningDeltaDraft
    | ModelReasoningCompletedDraft
    | ModelContentDeltaDraft
    | ModelToolCallStartedDraft
    | ModelToolCallDeltaDraft
    | ModelToolCallCompletedDraft
    | RunInvocationStartedDraft
    | RunInvocationEndedDraft
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
RunResumed = _stored("RunResumed", RunResumedDraft)
RunPaused = _stored("RunPaused", RunPausedDraft)
RunCancelled = _stored("RunCancelled", RunCancelledDraft)
RunFailed = _stored("RunFailed", RunFailedDraft)
RunSucceeded = _stored("RunSucceeded", RunSucceededDraft)
StepStarted = _stored("StepStarted", StepStartedDraft)
ModelCompleted = _stored("ModelCompleted", ModelCompletedDraft)
ModelFailed = _stored("ModelFailed", ModelFailedDraft)
ModelAborted = _stored("ModelAborted", ModelAbortedDraft)
ToolBatchPlanned = _stored("ToolBatchPlanned", ToolBatchPlannedDraft)
ToolProposed = _stored("ToolProposed", ToolProposedDraft)
ApprovalRequested = _stored("ApprovalRequested", ApprovalRequestedDraft)
ApprovalResolved = _stored("ApprovalResolved", ApprovalResolvedDraft)
ToolInvocationStarted = _stored("ToolInvocationStarted", ToolInvocationStartedDraft)
ToolInvocationCompleted = _stored(
    "ToolInvocationCompleted", ToolInvocationCompletedDraft
)
ToolInvocationMarkedUnknown = _stored(
    "ToolInvocationMarkedUnknown", ToolInvocationMarkedUnknownDraft
)
ToolBatchClosed = _stored("ToolBatchClosed", ToolBatchClosedDraft)
VerificationFailed = _stored("VerificationFailed", VerificationFailedDraft)
ContextCompacted = _stored("ContextCompacted", ContextCompactedDraft)
RunCheckpoint = _stored("RunCheckpoint", RunCheckpointDraft)


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
RunInvocationStarted = _transient(
    "RunInvocationStarted", RunInvocationStartedDraft
)
RunInvocationEnded = _transient("RunInvocationEnded", RunInvocationEndedDraft)


StoredRuntimeEvent = (
    RunCreated
    | UserMessage
    | RunResumed
    | RunPaused
    | RunCancelled
    | RunFailed
    | RunSucceeded
    | StepStarted
    | ModelCompleted
    | ModelFailed
    | ModelAborted
    | ToolBatchPlanned
    | ToolProposed
    | ApprovalRequested
    | ApprovalResolved
    | ToolInvocationStarted
    | ToolInvocationCompleted
    | ToolInvocationMarkedUnknown
    | ToolBatchClosed
    | VerificationFailed
    | ContextCompacted
    | RunCheckpoint
)

TransientRuntimeEvent = (
    ModelReasoningDelta
    | ModelReasoningCompleted
    | ModelContentDelta
    | ModelToolCallStarted
    | ModelToolCallDelta
    | ModelToolCallCompleted
    | RunInvocationStarted
    | RunInvocationEnded
)

RuntimeEvent = StoredRuntimeEvent | TransientRuntimeEvent
