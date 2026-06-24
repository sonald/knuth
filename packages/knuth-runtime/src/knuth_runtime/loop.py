from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall
from knuth.core.tools import ToolExecutionOutcome
from knuth.core.runtime_events import (
    ApprovalRequestedDraft,
    ConversationNoticeDraft,
    ContextSystemPreambleBuiltDraft,
    DurableRuntimeEventDraft,
    InterruptActivePhase,
    InterruptReason,
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
    RunInterruptedDraft,
    RunPausedDraft,
    RunSucceededDraft,
    StepStartedDraft,
    ToolBatchClosedDraft,
    ToolBatchPlannedDraft,
    ToolInvocationAwaitingExternalResultDraft,
    ToolInvocationCompletedDraft,
    ToolInvocationMarkedUnknownDraft,
    ToolInvocationStartedDraft,
    ToolProposedDraft,
    VerificationFailedDraft,
)
from knuth.core.types import ErrorInfo, RunStatus
from knuth_llmd import InferenceConfig, InferenceRuntimeOptions

from knuth_runtime.context import RunContext
from knuth_runtime.interrupts import shielded_ledger_writes
from knuth_runtime.invocation import RuntimeInvocation
from knuth_runtime.ledger import OpenToolBatch, RunLedgerState
from knuth_runtime.middleware import MessageMiddlewareCheckpoint

# Reasons that mean active work was deliberately stopped, so the abandoned
# attempt collapses to a durable INTERRUPTED fact rather than a resumable pause.
_ACTIVE_STOP_REASONS = frozenset(
    {"user_stop", "queued_user_prompt", "timeout", "shutdown", "hook_stop",
     "runtime_stop"}
)

_MODEL_INTERRUPT_NOTICE = (
    "The previous turn was stopped by the user before the assistant finished."
    " The interrupted response was discarded; do not silently retry the old"
    " action — wait for the user's next instruction."
)

_TOOL_INTERRUPT_NOTICE = (
    "The previous turn was stopped by the user while tools were running."
    " The tool observations above reflect what happened; do not silently retry"
    " the stopped actions — wait for the user's next instruction."
)


_ACTIVE_STATUSES_LOOP = frozenset({RunStatus.CREATED, RunStatus.RUNNING})


def _interrupt_reason(reason: str | None) -> InterruptReason:
    """Map a signal reason onto the durable vocabulary, defaulting to user_stop."""
    if reason in _ACTIVE_STOP_REASONS:
        return reason  # type: ignore[return-value]
    return "user_stop"


async def _emit_interrupt_collapse(
    invocation: RuntimeInvocation,
    *,
    reason: InterruptReason,
    active_phase: InterruptActivePhase,
    notice: str | None,
    leading: list[DurableRuntimeEventDraft] | None = None,
) -> None:
    """Commit an interrupt safe point as one atomic durable transaction.

    ``leading`` carries any phase-specific facts (model.aborted, tool
    observations, batch closure) that must land with the notice and
    ``run.interrupted`` so a crash or force-stop never sees half the collapse.
    """
    drafts: list[DurableRuntimeEventDraft] = list(leading or [])
    if notice is not None:
        drafts.append(ConversationNoticeDraft(kind="interrupted", content=notice))
    drafts.append(
        RunInterruptedDraft(reason=reason, active_phase=active_phase)
    )
    # Shield the durable writes: if backing cancellation woke the await that got
    # us here, the unwind must not swallow the facts that close the work.
    with shielded_ledger_writes():
        await invocation.emit_many(drafts)


async def _interrupt_at_loop_boundary(invocation: RuntimeInvocation) -> None:
    reason = _interrupt_reason(invocation.interrupt_signal.reason)
    await _emit_interrupt_collapse(
        invocation,
        reason=reason,
        active_phase="loop",
        # No active model/tool work to summarize between turns; the conversation
        # already ends at a clean boundary, so no notice is required.
        notice=None,
    )

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
        RunStatus.WAITING_TOOL_RESULT,
        RunStatus.INTERRUPTED,
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
    # Carry the invocation's interrupt signal into the model boundary so llmd
    # can wake its initial await and observe the signal between chunks.
    runtime_options = (runtime_options or InferenceRuntimeOptions()).model_copy(
        update={"abort_signal": invocation.interrupt_signal}
    )

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

        # Loop-level safe point: a signal that fired between turns (no active
        # model/tool await to wake) collapses here before any new work starts.
        if invocation.interrupt_signal.interrupted and status in _ACTIVE_STATUSES_LOOP:
            await _interrupt_at_loop_boundary(invocation)
            return RunStatus.INTERRUPTED

        # Budget on committed model turns, not raw attempts: interrupted
        # attempts bump ``steps`` but never ``committed_turns``, so repeated
        # Ctrl+C cannot push the run to max_turns_exceeded.
        if state.run.committed_turns >= state.run.max_turns:
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
    )
    if services.message_middleware_runner is not None:
        await services.message_middleware_runner.run_checkpoint(
            run_id,
            MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST,
        )
    view = await services.context_builder.build(
        ctx,
        model_config_fingerprint=inference_config.model_dump_json(
            exclude={"run_id", "trace_id"}
        ),
    )
    assert view.snapshot is not None
    preamble = None
    if view.messages and view.messages[0].role == InferenceRole.SYSTEM:
        preamble = view.messages[0].content
    await invocation.emit(ContextSystemPreambleBuiltDraft(content=preamble))
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
                # Only a user-driven stop becomes a durable interrupt. The safe
                # point verifies the live signal actually fired (or the reason
                # is an active-stop reason) before collapsing; a provider's own
                # or non-user abort is recorded and routed to a resumable pause,
                # not forged into ``run.interrupted``.
                signalled = invocation.interrupt_signal.interrupted
                is_active_stop = event.reason in _ACTIVE_STOP_REASONS
                if signalled or is_active_stop:
                    # The discarded assistant partial never reaches durable
                    # conversation, so the model needs a notice next turn. No
                    # open batch yet, so the collapse is the model.aborted fact,
                    # the notice, and run.interrupted — atomic.
                    await _emit_interrupt_collapse(
                        invocation,
                        reason=_interrupt_reason(
                            invocation.interrupt_signal.reason or event.reason
                        ),
                        active_phase="model",
                        notice=_MODEL_INTERRUPT_NOTICE,
                        leading=[
                            ModelAbortedDraft(step_id=step_id, reason=event.reason)
                        ],
                    )
                    return RunStatus.INTERRUPTED
                # Non-user abort: record it and pause for recovery rather than
                # discarding the turn as a user interrupt.
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
            tool_call_id=call.effective_id,
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
        await _run_after_turn_closed(invocation)
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


async def settle_crashed_invocations(
    emit: Callable[..., Awaitable[object]],
    batch: OpenToolBatch,
    after_completion: Callable[[], Awaitable[object]] | None = None,
) -> tuple[int, int]:
    """Crash recovery, split by effect (design §5.2): retryable effects fail
    loudly so the model can retry; external writes may have happened and
    become UNKNOWN pending human resolution.

    Returns ``(failed, unknown)`` counts. Shared by the loop's in-batch
    recovery and the explicit ``recover`` control surface.
    """
    failed = unknown = 0
    for inv in batch.by_status(ToolInvocationStatus.RUNNING):
        if inv.effect in EXTERNAL_EFFECTS:
            await emit(
                ToolInvocationMarkedUnknownDraft(
                    tool_call_id=inv.tool_call_id,
                    reason="runtime crashed while the tool was running;"
                    " the external side effect may or may not have happened",
                )
            )
            unknown += 1
        else:
            await emit(
                ToolInvocationCompletedDraft(
                    tool_call_id=inv.tool_call_id,
                    tool_name=inv.tool_name,
                    outcome="failed",
                    observation=_CRASH_OBSERVATION,
                    tool_status="crashed",
                )
            )
            if after_completion is not None:
                await after_completion()
            failed += 1
    return failed, unknown


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

    await settle_crashed_invocations(
        invocation.emit,
        batch,
        lambda: _run_after_tool_result_committed(invocation),
    )

    # Propose: pure decision, safe to repeat after a crash.
    for inv in batch.by_status(ToolInvocationStatus.PROPOSED):
        proposal = await services.tool_broker.propose(
            run_id,
            inv.tool_name,
            inv.args,
        )
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
                    approval_preview={"tool": inv.tool_name, "args": inv.args},
                )
            )

    # Re-read: the proposals above moved invocation states.
    state = await services.ledger.run_state(run_id)
    batch = state.open_batch
    assert batch is not None

    # Denied invocations need a model-visible observation before the batch
    # can close; the denial text lets the model recover next turn.
    for inv in batch.invocations:
        if (
            inv.status == ToolInvocationStatus.DENIED
            and not inv.observation_recorded
        ):
            completion = ToolInvocationCompletedDraft(
                tool_call_id=inv.tool_call_id,
                tool_name=inv.tool_name,
                outcome="denied",
                observation=(
                    f"Tool call denied: {inv.denied_observation or 'denied'}"
                ),
            )
            await _commit_completion_artifacts(invocation, completion)
            await invocation.emit(completion)
            await _run_after_tool_result_committed(invocation)

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
                        approval_preview={"tool": inv.tool_name, "args": inv.args},
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

    for inv in batch.by_status(ToolInvocationStatus.APPROVED):
        if not await services.tool_broker.awaits_external_result(inv):
            continue
        await invocation.emit(
            ToolInvocationAwaitingExternalResultDraft(
                tool_call_id=inv.tool_call_id,
                tool_name=inv.tool_name,
                args=inv.args,
            )
        )

    state = await services.ledger.run_state(run_id)
    batch = state.open_batch
    assert batch is not None
    if batch.by_status(ToolInvocationStatus.WAITING_TOOL_RESULT):
        return RunStatus.WAITING_TOOL_RESULT

    # Execute the approved calls serially in plan order, cooperating with the
    # interrupt signal. A user stop ends the whole turn, not just one tool.
    approved = list(batch.by_status(ToolInvocationStatus.APPROVED))
    for position, inv in enumerate(approved):
        if invocation.interrupt_signal.interrupted:
            # Caught before this call started: abandon it and the rest, then
            # collapse to INTERRUPTED atomically.
            await _collapse_tool_interrupt(
                invocation, batch, abandoned=approved[position:]
            )
            await _run_after_tool_result_committed(invocation)
            return RunStatus.INTERRUPTED

        await invocation.emit(
            ToolInvocationStartedDraft(
                tool_call_id=inv.tool_call_id,
                attempt=inv.attempt + 1,
            )
        )
        result = await services.tool_broker.execute(
            inv, signal=invocation.interrupt_signal
        )

        if result.outcome == ToolExecutionOutcome.UNKNOWN:
            # Indeterminate side effect: mark unknown, abandon the rest so a
            # later resume cannot run a stopped turn's remaining tools, then let
            # the UNKNOWN gate below pause for human recovery.
            await invocation.emit(
                ToolInvocationMarkedUnknownDraft(
                    tool_call_id=inv.tool_call_id,
                    reason=result.reason or "indeterminate tool outcome",
                )
            )
            await _abandon_unstarted(invocation, approved[position + 1 :])
            break

        completion = _completion_for(inv, result)
        if result.outcome == ToolExecutionOutcome.INTERRUPTED:
            # The active tool stopped cooperatively. Its observation, the
            # abandoned observations, batch close, notice, and run.interrupted
            # are one atomic collapse.
            # Commit artifacts to the store *before* the durable event that
            # references them. A crash in this window then leaves a harmless
            # committed-but-unreferenced artifact, never a durable event that
            # points at a still-pending artifact orphan GC could later delete.
            await _commit_completion_artifacts(invocation, completion)
            await _collapse_tool_interrupt(
                invocation,
                batch,
                abandoned=approved[position + 1 :],
                active=completion,
            )
            await _run_after_tool_result_committed(invocation)
            return RunStatus.INTERRUPTED
        await _commit_completion_artifacts(invocation, completion)
        await invocation.emit(completion)
        await _run_after_tool_result_committed(invocation)

    # Re-read: a tool may have become UNKNOWN above and now gates the batch.
    state = await services.ledger.run_state(run_id)
    batch = state.open_batch
    assert batch is not None
    if batch.by_status(ToolInvocationStatus.UNKNOWN):
        await invocation.emit(
            RunPausedDraft(
                reason="unknown_tool_outcome: resolve with `knuth resolve <tool_call_id>`",
            )
        )
        return RunStatus.PAUSED

    await invocation.emit(ToolBatchClosedDraft(batch_id=batch.batch_id))
    return None


async def _run_after_tool_result_committed(invocation: RuntimeInvocation) -> None:
    runner = invocation.services.message_middleware_runner
    if runner is None:
        return
    try:
        await runner.run_checkpoint(
            invocation.run_id,
            MessageMiddlewareCheckpoint.AFTER_TOOL_RESULT_COMMITTED,
        )
    except Exception:
        # This checkpoint is an opportunistic write; BEFORE_MODEL_REQUEST is the
        # blocking reconciliation point before another model call.
        return


async def _commit_completion_artifacts(
    invocation: RuntimeInvocation,
    completion: ToolInvocationCompletedDraft,
) -> None:
    """Flip the tool's archived artifacts from ``pending`` to ``committed``.

    Must run *before* the durable ``tool.invocation_completed`` that references
    them, so a crash in the window fails safe: a committed-but-unreferenced
    artifact (reclaimed only by ``reclaim_run``) rather than a durable event
    pointing at a ``pending`` artifact that orphan GC could later delete.
    """
    if not completion.raw_artifacts:
        return
    await invocation.services.artifact_store.mark_committed(
        invocation.run_id,
        list(completion.raw_artifacts),
    )


async def _run_after_turn_closed(invocation: RuntimeInvocation) -> None:
    runner = invocation.services.message_middleware_runner
    if runner is not None:
        try:
            await runner.run_checkpoint(
                invocation.run_id,
                MessageMiddlewareCheckpoint.AFTER_TURN_CLOSED,
            )
        except Exception:
            # Middleware failures here are non-fatal; the BEFORE_MODEL_REQUEST
            # boundary will retry on the next turn. The projection-checkpoint
            # writer still gets a chance below because its safe-boundary check
            # consults durable run state, not the middleware outcome.
            pass
    writer = invocation.services.projection_checkpoint_writer
    if writer is None:
        return
    try:
        await writer.maybe_append(invocation.run_id)
    except Exception:
        # Writer is a maintenance cache. Any failure is logged inside the
        # writer; surface nothing to the loop.
        return


def _completion_for(
    inv: ToolInvocation,
    result,
) -> ToolInvocationCompletedDraft:
    """Build the durable completion for a finished tool."""
    observation = result.to_observation_text()
    outcome_tag = (
        "succeeded"
        if result.outcome == ToolExecutionOutcome.SUCCEEDED
        else "failed"
        if result.outcome == ToolExecutionOutcome.FAILED
        else "interrupted"
    )
    raw_artifacts = list(result.result.artifacts) if result.result is not None else []
    self_condensed = result.result.condensed if result.result is not None else False
    tool_status = result.tool_status or (
        result.result.status.value if result.result is not None else None
    )
    return ToolInvocationCompletedDraft(
        tool_call_id=inv.tool_call_id,
        tool_name=inv.tool_name,
        outcome=outcome_tag,
        observation=observation,
        raw_artifacts=raw_artifacts,
        self_condensed=self_condensed,
        tool_status=tool_status,
    )


def _abandon_draft(inv: ToolInvocation) -> ToolInvocationCompletedDraft:
    return ToolInvocationCompletedDraft(
        tool_call_id=inv.tool_call_id,
        tool_name=inv.tool_name,
        outcome="interrupted",
        observation=(
            f"Tool {inv.tool_name} was not executed: the user stopped this turn"
            " before it ran."
        ),
        tool_status="abandoned",
    )


async def _abandon_unstarted(
    invocation: RuntimeInvocation, unstarted: list[ToolInvocation]
) -> None:
    """Give unstarted invocations an interrupted observation (non-collapse path).

    Used when the active tool is UNKNOWN: the batch cannot close cleanly, but
    the remaining tools must still be abandoned so recovery never runs them.
    """
    for inv in unstarted:
        await invocation.emit(_abandon_draft(inv))
        await _run_after_tool_result_committed(invocation)


async def _collapse_tool_interrupt(
    invocation: RuntimeInvocation,
    batch: OpenToolBatch,
    *,
    abandoned: list[ToolInvocation],
    active: ToolInvocationCompletedDraft | None = None,
) -> None:
    """Atomically close an interrupted tool batch and write the interrupt fact.

    Observations for the active and abandoned invocations, ``tool.batch_closed``,
    the user-stop notice, and ``run.interrupted`` commit in one transaction, so a
    crash or force-stop never leaves a closed batch without the interruption.
    """
    leading: list[DurableRuntimeEventDraft] = []
    if active is not None:
        leading.append(active)
    leading.extend(_abandon_draft(inv) for inv in abandoned)
    leading.append(ToolBatchClosedDraft(batch_id=batch.batch_id))
    await _emit_interrupt_collapse(
        invocation,
        reason=_interrupt_reason(invocation.interrupt_signal.reason),
        active_phase="tool",
        notice=_TOOL_INTERRUPT_NOTICE,
        leading=leading,
    )


async def _has_pending_approval(
    services, run_id: str, inv: ToolInvocation
) -> bool:
    pending = await services.ledger.pending_approvals(run_id)
    return any(approval.tool_call_id == inv.tool_call_id for approval in pending)
