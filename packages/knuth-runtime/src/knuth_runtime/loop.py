from __future__ import annotations

from uuid import uuid4

from knuth.core.events import (
    InferenceAborted,
    InferenceContentDelta,
    InferenceFailed,
    InferenceGenerationCompleted,
    InferenceReasoningCompleted,
    InferenceReasoningDelta,
    InferenceToolCallCompleted,
    InferenceToolCallDelta,
    InferenceToolCallStarted,
)
from knuth.core.invocations import (
    EXTERNAL_EFFECTS,
    ToolCallDecision,
    ToolInvocation,
    ToolInvocationStatus,
    approval_id_for,
    args_hash_for,
)
from knuth.core.messages import InferenceMessage, ToolCall
from knuth.core.runtime_events import (
    ApprovalRequestedDraft,
    ModelAbortedDraft,
    ModelCompletedDraft,
    ModelContentDeltaDraft,
    ModelFailedDraft,
    ModelReasoningCompletedDraft,
    ModelReasoningDeltaDraft,
    ModelToolCallCompletedDraft,
    ModelToolCallDeltaDraft,
    ModelToolCallStartedDraft,
    PlannedToolCall,
    RunFailedDraft,
    RunPausedDraft,
    RunSucceededDraft,
    StepStartedDraft,
    ToolBatchClosedDraft,
    ToolBatchPlannedDraft,
    ToolInvocationCompletedDraft,
    ToolInvocationMarkedUnknownDraft,
    ToolInvocationStartedDraft,
    ToolProposedDraft,
    VerificationFailedDraft,
)
from knuth.core.types import ErrorInfo, RunStatus
from knuth_llmd import InferenceConfig, InferenceRuntimeOptions

from knuth_runtime.context import RunContext
from knuth_runtime.invocation import RuntimeInvocation
from knuth_runtime.ledger import OpenToolBatch, RunLedgerState

# Observations above this size are offloaded to the artifact side store; the
# event keeps a ref and a preview (design §1.2).
OBSERVATION_INLINE_LIMIT = 8 * 1024
_OBSERVATION_PREVIEW_CHARS = 512

EMPTY_ANSWER_FEEDBACK = (
    "Your previous answer was empty. Provide a concrete answer or call a tool."
)
_CRASH_OBSERVATION = (
    "Tool execution was interrupted before completion (runtime crashed or was"
    " stopped). The tool did not record a result; retry if still needed."
)

_WAITING_OR_TERMINAL = frozenset(
    {
        RunStatus.PAUSED,
        RunStatus.WAITING_APPROVAL,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


async def run_agent_loop(
    invocation: RuntimeInvocation,
    inference_config: InferenceConfig,
    runtime_options: InferenceRuntimeOptions | None = None,
) -> RunStatus:
    """Scheduler over ledger projections: no log scanning, no heuristics.

    Every iteration re-reads ``run_state``; an open ToolBatch is the resume
    point after approval, pause, or crash.
    """
    run_id = invocation.run_id
    services = invocation.services

    while True:
        state = await services.ledger.run_state(run_id)
        status = state.run.status

        if status in _WAITING_OR_TERMINAL:
            return status

        if state.open_batch is not None:
            batch_status = await _drive_open_batch(invocation, state)
            if batch_status is not None:
                return batch_status
            continue

        if state.run.steps >= state.run.max_turns:
            await invocation.emit(
                RunFailedDraft(
                    error=ErrorInfo(
                        code="max_turns_exceeded",
                        message=f"Run exceeded max_turns={state.run.max_turns}",
                        details={"max_turns": state.run.max_turns},
                    )
                )
            )
            return RunStatus.FAILED

        step_status = await _run_step(invocation, state, inference_config, runtime_options)
        if step_status is not None:
            return step_status


async def _run_step(
    invocation: RuntimeInvocation,
    state: RunLedgerState,
    inference_config: InferenceConfig,
    runtime_options: InferenceRuntimeOptions | None,
) -> RunStatus | None:
    run_id = invocation.run_id
    services = invocation.services
    step_id = f"step_{uuid4().hex}"

    ctx = RunContext(
        run_id=run_id,
        user_id=state.run.user_id,
        workspace_uri=state.run.metadata.get("workspace_uri"),
    )
    view = await services.context_builder.build(
        ctx,
        model_config_fingerprint=inference_config.model_dump_json(
            exclude={"run_id", "trace_id"}
        ),
    )
    assert view.snapshot is not None
    await invocation.emit(
        StepStartedDraft(
            step_id=step_id,
            index=state.run.steps + 1,
            snapshot=view.snapshot,
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
        match event:
            case InferenceReasoningDelta():
                await invocation.emit(ModelReasoningDeltaDraft(delta=event.delta))
            case InferenceReasoningCompleted():
                await invocation.emit(ModelReasoningCompletedDraft())
            case InferenceContentDelta():
                await invocation.emit(ModelContentDeltaDraft(delta=event.delta))
            case InferenceToolCallStarted():
                await invocation.emit(
                    ModelToolCallStartedDraft(index=event.index, tool_call_id=event.id)
                )
            case InferenceToolCallDelta():
                await invocation.emit(
                    ModelToolCallDeltaDraft(
                        index=event.index,
                        tool_call_id=event.id,
                        name_delta=event.name_delta,
                        arguments_json_delta=event.arguments_json_delta,
                        raw=event.raw,
                    )
                )
            case InferenceToolCallCompleted():
                await invocation.emit(
                    ModelToolCallCompletedDraft(tool_call=event.tool_call)
                )
            case InferenceFailed():
                stream_error = event.error
                break
            case InferenceAborted():
                await invocation.emit(
                    ModelAbortedDraft(step_id=step_id, reason=event.reason)
                )
                await invocation.emit(
                    RunPausedDraft(reason=f"model_aborted: {event.reason}")
                )
                return RunStatus.PAUSED
            case InferenceGenerationCompleted():
                assistant_message = event.message
                finish_reason = event.finish_reason
                usage = event.usage

    if stream_error is not None:
        await invocation.emit(ModelFailedDraft(step_id=step_id, error=stream_error))
        await invocation.emit(RunFailedDraft(error=stream_error))
        return RunStatus.FAILED

    if assistant_message is None:
        error = ErrorInfo(
            code="missing_generation_end",
            message="inference stream ended without a generation completion",
        )
        await invocation.emit(ModelFailedDraft(step_id=step_id, error=error))
        await invocation.emit(RunFailedDraft(error=error))
        return RunStatus.FAILED

    # Persist only the minimal facts: provider raw payloads stay out of the
    # ledger by construction.
    tool_calls = [
        ToolCall(
            id=call.effective_id,
            name=call.name,
            arguments=call.arguments,
            index=call.index,
        )
        for call in assistant_message.tool_calls
    ]
    await invocation.emit(
        ModelCompletedDraft(
            step_id=step_id,
            content=assistant_message.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
    )

    if tool_calls:
        await invocation.emit(
            ToolBatchPlannedDraft(
                batch_id=f"batch_{uuid4().hex}",
                step_id=step_id,
                calls=[
                    PlannedToolCall(
                        tool_call_id=call.effective_id,
                        index=call.index,
                        name=call.name,
                        args=call.arguments,
                        args_hash=args_hash_for(call.arguments),
                    )
                    for call in tool_calls
                ],
            )
        )
        return None

    if assistant_message.content and assistant_message.content.strip():
        await invocation.emit(
            RunSucceededDraft(
                answer=assistant_message.content,
                turns=state.run.steps + 1,
            )
        )
        return RunStatus.SUCCEEDED

    # Verification failure must carry feedback the model will see next turn;
    # the ledger rejects the event otherwise.
    await invocation.emit(
        VerificationFailedDraft(
            reason="empty_final_answer",
            feedback=EMPTY_ANSWER_FEEDBACK,
        )
    )
    return None


async def _drive_open_batch(
    invocation: RuntimeInvocation,
    state: RunLedgerState,
) -> RunStatus | None:
    """Advance the open ToolBatch: recover crashed work, propose, gate on
    approvals and unknown outcomes, execute, observe, close."""
    run_id = invocation.run_id
    services = invocation.services
    batch = state.open_batch
    assert batch is not None

    # Crash recovery, split by effect: retryable effects fail loudly so the
    # model can retry; external writes may have happened and become UNKNOWN.
    for inv in batch.by_status(ToolInvocationStatus.RUNNING):
        if inv.effect in EXTERNAL_EFFECTS:
            await invocation.emit(
                ToolInvocationMarkedUnknownDraft(
                    tool_call_id=inv.tool_call_id,
                    reason="runtime crashed while the tool was running;"
                    " the external side effect may or may not have happened",
                )
            )
        else:
            await invocation.emit(
                ToolInvocationCompletedDraft(
                    tool_call_id=inv.tool_call_id,
                    tool_name=inv.tool_name,
                    outcome="failed",
                    observation=_CRASH_OBSERVATION,
                    meta={"reason": "crashed"},
                )
            )

    # Propose: pure decision, safe to repeat after a crash.
    for inv in batch.by_status(ToolInvocationStatus.PROPOSED):
        proposal = await services.tool_broker.propose(run_id, inv.tool_name, inv.args)
        await invocation.emit(
            ToolProposedDraft(
                tool_call_id=inv.tool_call_id,
                decision=proposal.decision,
                effect=proposal.effect,
                risk=proposal.risk,
                error=proposal.error,
            )
        )
        if proposal.decision == ToolCallDecision.REQUIRES_APPROVAL:
            await invocation.emit(
                ApprovalRequestedDraft(
                    approval_id=approval_id_for(run_id, inv.tool_call_id),
                    tool_call_id=inv.tool_call_id,
                    args_hash=inv.args_hash,
                    title=proposal.approval_title
                    or f"Approve tool call: {inv.tool_name}",
                    reason=proposal.approval_reason
                    or f"Tool has effect={proposal.effect.value}, risk={proposal.risk.value}",
                    risk=proposal.risk.value,
                    preview={"tool": inv.tool_name, "args": inv.args},
                )
            )

    # Re-read: the proposals above moved invocation states.
    state = await services.ledger.run_state(run_id)
    batch = state.open_batch
    assert batch is not None

    # Denied invocations need a model-visible observation before the batch
    # can close; the denial text lets the model recover next turn.
    for inv in batch.invocations:
        if inv.status == ToolInvocationStatus.DENIED and not inv.observed:
            await invocation.emit(
                ToolInvocationCompletedDraft(
                    tool_call_id=inv.tool_call_id,
                    tool_name=inv.tool_name,
                    outcome="denied",
                    observation=f"Tool call denied: {inv.denial_reason or 'denied'}",
                )
            )

    # Awaiting approval gates the run. Crash recovery: re-request a missing
    # approval record with a generic title; policy facts are already frozen.
    awaiting = batch.by_status(ToolInvocationStatus.AWAITING_APPROVAL)
    if awaiting:
        for inv in awaiting:
            if not await _has_pending_approval(services, run_id, inv):
                await invocation.emit(
                    ApprovalRequestedDraft(
                        approval_id=approval_id_for(run_id, inv.tool_call_id),
                        tool_call_id=inv.tool_call_id,
                        args_hash=inv.args_hash,
                        title=f"Approve tool call: {inv.tool_name}",
                        reason=f"Tool has effect={inv.effect.value}, risk={inv.risk.value}",
                        risk=inv.risk.value,
                        preview={"tool": inv.tool_name, "args": inv.args},
                    )
                )
        return RunStatus.WAITING_APPROVAL

    # Unresolved UNKNOWN outcomes block the batch until a human resolves them.
    if batch.by_status(ToolInvocationStatus.UNKNOWN):
        await invocation.emit(
            RunPausedDraft(
                reason="unknown_tool_outcome: resolve with `knuth resolve <tool_call_id>`",
            )
        )
        return RunStatus.PAUSED

    # Execute the approved calls serially in plan order. Each invocation gets
    # an idempotency key so idempotent external APIs can dedupe retries.
    for inv in batch.by_status(ToolInvocationStatus.APPROVED):
        attempt = inv.attempts + 1
        idempotency_key = f"{run_id}:{inv.tool_call_id}:{attempt}"
        await invocation.emit(
            ToolInvocationStartedDraft(
                tool_call_id=inv.tool_call_id,
                idempotency_key=idempotency_key,
                attempt=attempt,
            )
        )
        result = await services.tool_broker.execute(
            inv.model_copy(update={"idempotency_key": idempotency_key})
        )
        observation = result.to_observation_text()
        observation_ref = None
        observation_preview = None
        if len(observation) > OBSERVATION_INLINE_LIMIT:
            artifact = await services.ledger.put_artifact(
                run_id, "tool_observation", observation
            )
            observation_ref = artifact.id
            observation_preview = observation[:_OBSERVATION_PREVIEW_CHARS]
            observation = None
        await invocation.emit(
            ToolInvocationCompletedDraft(
                tool_call_id=inv.tool_call_id,
                tool_name=inv.tool_name,
                outcome="succeeded" if result.ok else "failed",
                observation=observation,
                observation_ref=observation_ref,
                observation_preview=observation_preview,
                meta={"tool_status": result.status.value},
            )
        )

    await invocation.emit(ToolBatchClosedDraft(batch_id=batch.batch_id))
    return None


async def _has_pending_approval(
    services, run_id: str, inv: ToolInvocation
) -> bool:
    pending = await services.ledger.pending_approvals(run_id)
    return any(approval.tool_call_id == inv.tool_call_id for approval in pending)
