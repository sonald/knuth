from __future__ import annotations

from pydantic import TypeAdapter

from knuth.core.runtime_events import (
    ApprovalRequested,
    DurableRuntimeEventDraft,
    ModelAborted,
    ModelCompleted,
    ModelContentDelta,
    ModelFailed,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelStarted,
    ModelToolCallCompleted,
    ModelToolCallDelta,
    ModelToolCallStarted,
    RunCreated,
    RunFailed,
    RunSucceeded,
    StoredRuntimeEvent,
    StoredRuntimeEventBase,
    ToolCompleted,
    ToolIntentEvent,
    ToolProposed,
    ToolStarted,
    TransientRuntimeEvent,
    TransientRuntimeEventBase,
    TransientRuntimeEventDraft,
    UserMessage,
    VerificationFailed,
)

_STORED_EVENT_BY_TYPE: dict[str, type[StoredRuntimeEventBase]] = {
    "run.created": RunCreated,
    "user.message": UserMessage,
    "model.started": ModelStarted,
    "model.completed": ModelCompleted,
    "model.aborted": ModelAborted,
    "model.failed": ModelFailed,
    "tool.intent": ToolIntentEvent,
    "tool.proposed": ToolProposed,
    "tool.started": ToolStarted,
    "tool.completed": ToolCompleted,
    "approval.requested": ApprovalRequested,
    "run.succeeded": RunSucceeded,
    "run.failed": RunFailed,
    "verification.failed": VerificationFailed,
}

_TRANSIENT_EVENT_BY_TYPE: dict[str, type[TransientRuntimeEventBase]] = {
    "model.reasoning.delta": ModelReasoningDelta,
    "model.reasoning.completed": ModelReasoningCompleted,
    "model.content.delta": ModelContentDelta,
    "model.tool_call.started": ModelToolCallStarted,
    "model.tool_call.delta": ModelToolCallDelta,
    "model.tool_call.completed": ModelToolCallCompleted,
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
