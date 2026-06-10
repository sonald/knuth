from __future__ import annotations

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
    RuntimeEventDraft,
    ToolCompletedDraft,
    ToolIntentDraft,
    ToolProposedDraft,
    ToolStartedDraft,
    VerificationFailedDraft,
)
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.tools import ToolIntent, ToolProposalStatus
from knuth.core.types import ErrorInfo, RunStatus
from knuth_llmd import InferenceConfig, InferenceRuntimeOptions
from knuth_runtime.context import RunContext
from knuth_runtime.invocation import RuntimeInvocation
from knuth_runtime.services import RuntimeServices


async def run_agent_loop(
    invocation: RuntimeInvocation,
    inference_config: InferenceConfig,
    runtime_options: InferenceRuntimeOptions | None = None,
) -> RunStatus:
    run_id = invocation.run_id
    services = invocation.services
    turns = 0

    async def emit(event: RuntimeEventDraft) -> RuntimeEvent:
        return await invocation.emit(event)

    while True:
        run = await services.run_store.get(run_id)
        if run.status in {
            RunStatus.PAUSED,
            RunStatus.WAITING_APPROVAL,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run.status

        if run.status == RunStatus.CREATED:
            await services.run_store.set_status(run_id, RunStatus.RUNNING)

        pending_message = await _pending_assistant_tool_message(run_id, services)
        if pending_message is not None:
            status = await handle_tool_calls(invocation, pending_message)
            if status is not None:
                return status
            continue

        if turns >= run.max_turns:
            await emit(
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
        await emit(
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
                await emit(ModelReasoningDeltaDraft(delta=event.delta))
            elif isinstance(event, InferenceReasoningCompleted):
                await emit(ModelReasoningCompletedDraft())
            elif isinstance(event, InferenceContentDelta):
                await emit(ModelContentDeltaDraft(delta=event.delta))
            elif isinstance(event, InferenceToolCallStarted):
                await emit(
                    ModelToolCallStartedDraft(
                        index=event.index,
                        tool_call_id=event.id,
                    )
                )
            elif isinstance(event, InferenceToolCallDelta):
                await emit(
                    ModelToolCallDeltaDraft(
                        index=event.index,
                        tool_call_id=event.id,
                        name_delta=event.name_delta,
                        arguments_json_delta=event.arguments_json_delta,
                        raw=event.raw,
                    )
                )
            elif isinstance(event, InferenceToolCallCompleted):
                await emit(
                    ModelToolCallCompletedDraft(tool_call=event.tool_call)
                )
            elif isinstance(event, InferenceFailed):
                stream_error = event.error
                break
            elif isinstance(event, InferenceAborted):
                await emit(ModelAbortedDraft(reason=event.reason))
                await services.run_store.set_status(run_id, RunStatus.PAUSED)
                return RunStatus.PAUSED
            elif isinstance(event, InferenceGenerationCompleted):
                assistant_message = event.message
                finish_reason = event.finish_reason
                usage = event.usage

        if stream_error is not None:
            await emit(ModelFailedDraft(error=stream_error))
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        if assistant_message is None:
            await emit(
                ModelFailedDraft(
                    error=ErrorInfo(
                        code="missing_generation_end",
                        message="missing_generation_end",
                    )
                )
            )
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        await emit(
            ModelCompletedDraft(
                turn=turns,
                message=assistant_message,
                finish_reason=finish_reason,
                usage=usage,
            )
        )

        if assistant_message.tool_calls:
            status = await handle_tool_calls(invocation, assistant_message)
            if status is not None:
                return status
            continue

        if assistant_message.content and assistant_message.content.strip():
            await emit(
                RunSucceededDraft(answer=assistant_message.content or "", turns=turns)
            )
            await services.run_store.set_status(run_id, RunStatus.SUCCEEDED)
            return RunStatus.SUCCEEDED

        await emit(VerificationFailedDraft(reason="empty_final_answer"))


async def handle_tool_calls(
    invocation: RuntimeInvocation,
    assistant_message: InferenceMessage,
) -> RunStatus | None:
    run_id = invocation.run_id
    services = invocation.services

    async def emit(event: DurableRuntimeEventDraft) -> RuntimeEvent:
        return await invocation.emit(event)

    intents = [ToolIntent.from_tool_call(call) for call in assistant_message.tool_calls]
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
