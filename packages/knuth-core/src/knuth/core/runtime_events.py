from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from knuth.core.inference_events import UsageInfo
from knuth.core.invocations import (
    ToolCallDecision,
    ToolEffect,
    ToolRisk,
)
from knuth.core.messages import InferenceMessage, ToolCall
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


class TapePosition(KnuthModel):
    kind: Literal["before", "after", "boundary"]
    target_id: str | None = None
    boundary: Literal[
        "conversation_start", "conversation_end", "before_model_request"
    ] | None = None


def ledger_message_id(seq: int) -> str:
    return f"m:{seq}"


def rewrite_id_for_begin_seq(seq: int) -> str:
    return f"rw:{seq}"


def rewrite_message_id(rewrite_id: str, ordinal: int) -> str:
    return f"{rewrite_id}#{ordinal}"


class RuntimeEventDraftBase(KnuthModel):
    type: str
    durability: EventDurability = EventDurability.DURABLE


class RunCreatedDraft(RuntimeEventDraftBase):
    type: Literal["run.created"] = "run.created"
    query: str


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


InterruptReason = Literal[
    "user_stop",
    "queued_user_prompt",
    "timeout",
    "shutdown",
    "hook_stop",
    "runtime_stop",
]

InterruptActivePhase = Literal["model", "tool", "loop", "unknown"]


class RunInterruptedDraft(RuntimeEventDraftBase):
    type: Literal["run.interrupted"] = "run.interrupted"
    reason: InterruptReason
    active_phase: InterruptActivePhase
    message: str | None = None


class ConversationNoticeDraft(RuntimeEventDraftBase):
    type: Literal["conversation.notice"] = "conversation.notice"
    kind: Literal["interrupted", "runtime"]
    content: str


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
    approval_preview: dict[str, Any] = Field(default_factory=dict)


class ApprovalResolvedDraft(RuntimeEventDraftBase):
    type: Literal["approval.resolved"] = "approval.resolved"
    approval_id: str
    resolution: Literal["approved", "denied"]
    resolved_by: str | None = None


class ToolInvocationStartedDraft(RuntimeEventDraftBase):
    type: Literal["tool.invocation_started"] = "tool.invocation_started"
    tool_call_id: str
    attempt: int = 1


class ToolInvocationAwaitingExternalResultDraft(RuntimeEventDraftBase):
    type: Literal["tool.invocation_awaiting_external_result"] = (
        "tool.invocation_awaiting_external_result"
    )
    tool_call_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationCompletedDraft(RuntimeEventDraftBase):
    type: Literal["tool.invocation_completed"] = "tool.invocation_completed"
    tool_call_id: str
    tool_name: str
    outcome: Literal["succeeded", "failed", "denied", "interrupted"]
    observation: str | None = None
    artifact_ref: str | None = None
    observation_preview: str | None = None
    tool_status: str | None = None


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


class MessageRewriteAnchorDraft(RuntimeEventDraftBase):
    type: Literal["message.rewrite_anchor"] = "message.rewrite_anchor"
    kind: Literal["begin", "end"]
    middleware: str
    operation: Literal["insert", "replace"]
    position: TapePosition | None = None
    suppresses: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageRewriteMessageDraft(RuntimeEventDraftBase):
    type: Literal["message.rewrite_message"] = "message.rewrite_message"
    message: InferenceMessage
    metadata: dict[str, Any] = Field(default_factory=dict)


DurableRuntimeEventDraft = (
    RunCreatedDraft
    | UserMessageDraft
    | RunResumedDraft
    | RunPausedDraft
    | RunInterruptedDraft
    | ConversationNoticeDraft
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
    | ToolInvocationAwaitingExternalResultDraft
    | ToolInvocationCompletedDraft
    | ToolInvocationMarkedUnknownDraft
    | ToolBatchClosedDraft
    | VerificationFailedDraft
    | MessageRewriteAnchorDraft
    | MessageRewriteMessageDraft
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


# Stored events are draft + storage envelope. The draft comes first so its
# ``type: Literal[...]`` wins field resolution: that literal tag is what makes
# the discriminated unions below parse each event back to its own class.


class RunCreated(RunCreatedDraft, StoredRuntimeEventBase):
    pass


class UserMessage(UserMessageDraft, StoredRuntimeEventBase):
    pass


class RunResumed(RunResumedDraft, StoredRuntimeEventBase):
    pass


class RunPaused(RunPausedDraft, StoredRuntimeEventBase):
    pass


class RunInterrupted(RunInterruptedDraft, StoredRuntimeEventBase):
    pass


class ConversationNotice(ConversationNoticeDraft, StoredRuntimeEventBase):
    pass


class RunCancelled(RunCancelledDraft, StoredRuntimeEventBase):
    pass


class RunFailed(RunFailedDraft, StoredRuntimeEventBase):
    pass


class RunSucceeded(RunSucceededDraft, StoredRuntimeEventBase):
    pass


class StepStarted(StepStartedDraft, StoredRuntimeEventBase):
    pass


class ModelCompleted(ModelCompletedDraft, StoredRuntimeEventBase):
    pass


class ModelFailed(ModelFailedDraft, StoredRuntimeEventBase):
    pass


class ModelAborted(ModelAbortedDraft, StoredRuntimeEventBase):
    pass


class ToolBatchPlanned(ToolBatchPlannedDraft, StoredRuntimeEventBase):
    pass


class ToolProposed(ToolProposedDraft, StoredRuntimeEventBase):
    pass


class ApprovalRequested(ApprovalRequestedDraft, StoredRuntimeEventBase):
    pass


class ApprovalResolved(ApprovalResolvedDraft, StoredRuntimeEventBase):
    pass


class ToolInvocationStarted(ToolInvocationStartedDraft, StoredRuntimeEventBase):
    pass


class ToolInvocationAwaitingExternalResult(
    ToolInvocationAwaitingExternalResultDraft, StoredRuntimeEventBase
):
    pass


class ToolInvocationCompleted(ToolInvocationCompletedDraft, StoredRuntimeEventBase):
    pass


class ToolInvocationMarkedUnknown(
    ToolInvocationMarkedUnknownDraft, StoredRuntimeEventBase
):
    pass


class ToolBatchClosed(ToolBatchClosedDraft, StoredRuntimeEventBase):
    pass


class VerificationFailed(VerificationFailedDraft, StoredRuntimeEventBase):
    pass


class MessageRewriteAnchor(MessageRewriteAnchorDraft, StoredRuntimeEventBase):
    rewrite_id: str


class MessageRewriteMessage(MessageRewriteMessageDraft, StoredRuntimeEventBase):
    rewrite_id: str
    message_id: str


class ModelReasoningDelta(ModelReasoningDeltaDraft, TransientRuntimeEventBase):
    pass


class ModelReasoningCompleted(ModelReasoningCompletedDraft, TransientRuntimeEventBase):
    pass


class ModelContentDelta(ModelContentDeltaDraft, TransientRuntimeEventBase):
    pass


class ModelToolCallStarted(ModelToolCallStartedDraft, TransientRuntimeEventBase):
    pass


class ModelToolCallDelta(ModelToolCallDeltaDraft, TransientRuntimeEventBase):
    pass


class ModelToolCallCompleted(ModelToolCallCompletedDraft, TransientRuntimeEventBase):
    pass


class RunInvocationStarted(RunInvocationStartedDraft, TransientRuntimeEventBase):
    pass


class RunInvocationEnded(RunInvocationEndedDraft, TransientRuntimeEventBase):
    pass


StoredRuntimeEvent = (
    RunCreated
    | UserMessage
    | RunResumed
    | RunPaused
    | RunInterrupted
    | ConversationNotice
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
    | ToolInvocationAwaitingExternalResult
    | ToolInvocationCompleted
    | ToolInvocationMarkedUnknown
    | ToolBatchClosed
    | VerificationFailed
    | MessageRewriteAnchor
    | MessageRewriteMessage
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
