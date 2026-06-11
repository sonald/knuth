from __future__ import annotations

from pydantic import TypeAdapter

from knuth.core.runtime_events import (
    ApprovalRequested,
    ApprovalResolved,
    DurableRuntimeEventDraft,
    ModelAborted,
    ModelCompleted,
    ModelContentDelta,
    ModelFailed,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelToolCallCompleted,
    ModelToolCallDelta,
    ModelToolCallStarted,
    RunCancelled,
    RunCreated,
    RunFailed,
    RunInvocationEnded,
    RunInvocationStarted,
    RunPaused,
    RunResumed,
    RunSucceeded,
    StepStarted,
    StoredRuntimeEvent,
    StoredRuntimeEventBase,
    ToolBatchClosed,
    ToolBatchPlanned,
    ToolInvocationCompleted,
    ToolInvocationMarkedUnknown,
    ToolInvocationStarted,
    ToolProposed,
    TransientRuntimeEvent,
    TransientRuntimeEventBase,
    TransientRuntimeEventDraft,
    UserMessage,
    VerificationFailed,
)

_STORED_EVENT_BY_TYPE: dict[str, type[StoredRuntimeEventBase]] = {
    "run.created": RunCreated,
    "user.message": UserMessage,
    "run.resumed": RunResumed,
    "run.paused": RunPaused,
    "run.cancelled": RunCancelled,
    "run.failed": RunFailed,
    "run.succeeded": RunSucceeded,
    "step.started": StepStarted,
    "model.completed": ModelCompleted,
    "model.failed": ModelFailed,
    "model.aborted": ModelAborted,
    "tool.batch_planned": ToolBatchPlanned,
    "tool.proposed": ToolProposed,
    "approval.requested": ApprovalRequested,
    "approval.resolved": ApprovalResolved,
    "tool.invocation_started": ToolInvocationStarted,
    "tool.invocation_completed": ToolInvocationCompleted,
    "tool.invocation_marked_unknown": ToolInvocationMarkedUnknown,
    "tool.batch_closed": ToolBatchClosed,
    "verification.failed": VerificationFailed,
}

_TRANSIENT_EVENT_BY_TYPE: dict[str, type[TransientRuntimeEventBase]] = {
    "model.reasoning.delta": ModelReasoningDelta,
    "model.reasoning.completed": ModelReasoningCompleted,
    "model.content.delta": ModelContentDelta,
    "model.tool_call.started": ModelToolCallStarted,
    "model.tool_call.delta": ModelToolCallDelta,
    "model.tool_call.completed": ModelToolCallCompleted,
    "run.invocation.started": RunInvocationStarted,
    "run.invocation.ended": RunInvocationEnded,
}

_STORED_RUNTIME_EVENT_ADAPTER = TypeAdapter(StoredRuntimeEvent)


def store_runtime_event(
    run_id: str,
    seq: int,
    event: DurableRuntimeEventDraft,
    *,
    event_id: str,
    created_at: str,
) -> StoredRuntimeEvent:
    event_class = _STORED_EVENT_BY_TYPE[event.type]
    return event_class(
        **event.model_dump(),
        id=event_id,
        run_id=run_id,
        seq=seq,
        created_at=created_at,
    )


def emit_transient_runtime_event(
    run_id: str,
    event: TransientRuntimeEventDraft,
    *,
    event_id: str,
    created_at: str,
) -> TransientRuntimeEvent:
    event_class = _TRANSIENT_EVENT_BY_TYPE[event.type]
    return event_class(
        **event.model_dump(),
        id=event_id,
        run_id=run_id,
        created_at=created_at,
    )


def parse_stored_runtime_event_json(data: str) -> StoredRuntimeEvent:
    return _STORED_RUNTIME_EVENT_ADAPTER.validate_json(data)
