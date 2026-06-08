from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from knuth.core.events import (
    ApprovalRequestedDraft,
    DurableRuntimeEventDraft,
    InferenceAborted,
    InferenceContentDelta,
    InferenceFailed,
    InferenceGenerationCompleted,
    InferenceReasoningCompleted,
    InferenceReasoningDelta,
    InferenceToolCallCompleted,
    InferenceToolCallDelta,
    InferenceToolCallStarted,
    ModelAbortedDraft,
    ModelCompletedDraft,
    ModelContentDeltaDraft,
    ModelFailedDraft,
    ModelReasoningCompletedDraft,
    ModelReasoningDeltaDraft,
    ModelStartedDraft,
    ModelToolCallCompletedDraft,
    ModelToolCallDeltaDraft,
    ModelToolCallStartedDraft,
    RunFailedDraft,
    RunSucceededDraft,
    RuntimeEvent,
    ToolCompletedDraft,
    ToolIntentDraft,
    ToolProposedDraft,
    ToolStartedDraft,
    TransientRuntimeEventDraft,
    UserInputRequestedDraft,
    VerificationFailedDraft,
    emit_transient_runtime_event,
)
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.tools import ToolIntent, ToolProposalStatus
from knuth.core.types import ErrorInfo, RunStatus
from knuth_llmd import InferenceConfig, InferenceRuntimeOptions
from knuth_runtime.context import RunContext
from knuth_runtime.services import RuntimeServices

EventSink = Callable[[RuntimeEvent], Awaitable[None]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


async def run_agent_loop(
    run_id: str,
    services: RuntimeServices,
    inference_config: InferenceConfig,
    runtime_options: InferenceRuntimeOptions | None = None,
    on_event: EventSink | None = None,
) -> RunStatus:
    turns = 0

    async def emit_durable(event: DurableRuntimeEventDraft) -> RuntimeEvent:
        stored_event = await services.event_store.append(run_id, event)
        if on_event is not None:
            await on_event(stored_event)
        return stored_event

    async def emit_transient(event: TransientRuntimeEventDraft) -> RuntimeEvent:
        runtime_event = emit_transient_runtime_event(
            run_id,
            event,
            event_id=f"evt_{uuid4().hex}",
            created_at=_utc_now(),
        )
        if on_event is not None:
            await on_event(runtime_event)
        return runtime_event

    while True:
        run = await services.run_store.get(run_id)
        if run.status in {
            RunStatus.PAUSED,
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_USER,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run.status

        if run.status == RunStatus.CREATED:
            await services.run_store.set_status(run_id, RunStatus.RUNNING)

        pending_message = await _pending_assistant_tool_message(run_id, services)
        if pending_message is not None:
            status = await handle_tool_calls(
                run_id, pending_message, services, on_event
            )
            if status is not None:
                return status
            continue

        if turns >= run.max_turns:
            await emit_durable(
                RunFailedDraft(
                    reason="max_turns_exceeded",
                    max_turns=run.max_turns,
                )
            )
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        turns += 1
        ctx = RunContext(
            run_id=run_id,
            user_id=run.user_id,
            workspace_uri=run.metadata.get("workspace_uri"),
        )
        view = await services.context_builder.build(ctx)
        await emit_durable(
            ModelStartedDraft(
                turn=turns,
                model=services.inference_client.model,
                message_count=len(view.messages),
                tool_count=len(view.tools),
            )
        )

        assistant_message: InferenceMessage | None = None
        finish_reason: str | None = None
        usage = None
        stream_error: ErrorInfo | None = None
        async for event in services.inference_client.stream(
            messages=view.messages,
            tools=view.tools,
            config=inference_config.model_copy(update={"run_id": run_id}),
            runtime=runtime_options,
        ):
            if isinstance(event, InferenceReasoningDelta):
                await emit_transient(ModelReasoningDeltaDraft(delta=event.delta))
            elif isinstance(event, InferenceReasoningCompleted):
                await emit_transient(ModelReasoningCompletedDraft())
            elif isinstance(event, InferenceContentDelta):
                await emit_transient(ModelContentDeltaDraft(delta=event.delta))
            elif isinstance(event, InferenceToolCallStarted):
                await emit_transient(
                    ModelToolCallStartedDraft(index=event.index, id=event.id)
                )
            elif isinstance(event, InferenceToolCallDelta):
                await emit_transient(
                    ModelToolCallDeltaDraft(
                        index=event.index,
                        id=event.id,
                        name_delta=event.name_delta,
                        arguments_json_delta=event.arguments_json_delta,
                        raw=event.raw,
                    )
                )
            elif isinstance(event, InferenceToolCallCompleted):
                await emit_transient(
                    ModelToolCallCompletedDraft(tool_call=event.tool_call)
                )
            elif isinstance(event, InferenceFailed):
                stream_error = event.error
                break
            elif isinstance(event, InferenceAborted):
                await emit_durable(ModelAbortedDraft(reason=event.reason))
                await services.run_store.set_status(run_id, RunStatus.PAUSED)
                return RunStatus.PAUSED
            elif isinstance(event, InferenceGenerationCompleted):
                assistant_message = event.message
                finish_reason = event.finish_reason
                usage = event.usage

        if stream_error is not None:
            await emit_durable(ModelFailedDraft(error=stream_error))
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        if assistant_message is None:
            await emit_durable(
                ModelFailedDraft(
                    error=ErrorInfo(
                        code="missing_generation_end",
                        message="missing_generation_end",
                    )
                )
            )
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        await emit_durable(
            ModelCompletedDraft(
                turn=turns,
                message=assistant_message,
                finish_reason=finish_reason,
                usage=usage,
            )
        )

        if assistant_message.tool_calls:
            status = await handle_tool_calls(
                run_id, assistant_message, services, on_event
            )
            if status is not None:
                return status
            continue

        if assistant_message.content and assistant_message.content.strip():
            await emit_durable(
                RunSucceededDraft(answer=assistant_message.content or "", turns=turns)
            )
            await services.run_store.set_status(run_id, RunStatus.SUCCEEDED)
            return RunStatus.SUCCEEDED

        await emit_durable(
            VerificationFailedDraft(reason="empty_final_answer")
        )


async def handle_tool_calls(
    run_id: str,
    assistant_message: InferenceMessage,
    services: RuntimeServices,
    on_event: EventSink | None = None,
) -> RunStatus | None:
    async def emit(event: DurableRuntimeEventDraft) -> RuntimeEvent:
        event = await services.event_store.append(run_id, event)
        if on_event is not None:
            await on_event(event)
        return event

    intents = [ToolIntent.from_tool_call(call) for call in assistant_message.tool_calls]
    for intent in intents:
        if intent.name == "knuth.ask_user":
            await emit(
                UserInputRequestedDraft(
                    question=str(intent.arguments.get("question", "")),
                    tool_call_id=intent.id,
                )
            )
            await services.run_store.set_status(run_id, RunStatus.WAITING_USER)
            return RunStatus.WAITING_USER

    proposals = []
    for intent in intents:
        await emit(ToolIntentDraft(intent=intent))
        proposal = await services.tool_broker.propose(run_id, intent)
        await emit(ToolProposedDraft(proposal=proposal))
        if proposal.status == ToolProposalStatus.DENIED:
            error_message = InferenceMessage(
                role=InferenceRole.TOOL_RESULT,
                tool_call_id=intent.id,
                tool_name=intent.name,
                content=f"Tool call denied: {proposal.error.message if proposal.error else 'unknown'}",
            )
            await emit(
                ToolCompletedDraft(
                    intent=intent,
                    message=error_message,
                    outcome="denied",
                )
            )
            continue
        if proposal.status == ToolProposalStatus.REQUIRES_APPROVAL:
            if proposal.approval is None:
                await services.run_store.set_status(run_id, RunStatus.FAILED)
                return RunStatus.FAILED
            approval = await services.approvals.request(proposal.approval)
            await emit(
                ApprovalRequestedDraft(
                    approval_id=approval.id,
                    tool_call_id=proposal.intent.id,
                    title=approval.title,
                    reason=approval.reason,
                    risk=approval.risk,
                )
            )
            await services.run_store.set_status(run_id, RunStatus.WAITING_APPROVAL)
            return RunStatus.WAITING_APPROVAL
        proposals.append(proposal)

    for proposal in proposals:
        await emit(ToolStartedDraft(intent=proposal.intent))
        record = await services.tool_broker.execute(run_id, proposal)
        await emit(
            ToolCompletedDraft(
                intent=proposal.intent,
                result=record.result,
                message=record.to_tool_result_message(),
                outcome="succeeded" if record.result.ok else "failed",
            )
        )
    return None


async def _pending_assistant_tool_message(
    run_id: str, services: RuntimeServices
) -> InferenceMessage | None:
    events = await services.event_store.list_events(run_id)
    latest_model: tuple[int, InferenceMessage] | None = None
    completed_ids: set[str] = set()
    for event in events:
        if event.type == "model.completed":
            if event.message.tool_calls:
                latest_model = (event.seq, event.message)
                completed_ids.clear()
        elif latest_model is not None and event.seq > latest_model[0] and event.type == "tool.completed":
            if event.message.tool_call_id is not None:
                completed_ids.add(event.message.tool_call_id)
    if latest_model is None:
        return None
    expected_ids = {call.id or f"call_{call.index}" for call in latest_model[1].tool_calls}
    if expected_ids and not expected_ids.issubset(completed_ids):
        return latest_model[1]
    return None
