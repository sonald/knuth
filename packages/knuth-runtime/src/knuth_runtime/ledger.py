from __future__ import annotations

import functools
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import anyio
from pydantic import BaseModel

from knuth.core.events import (
    DurableRuntimeEventDraft,
    StoredRuntimeEvent,
    parse_stored_runtime_event_json,
    store_runtime_event,
)
from knuth.core.invocations import (
    Approval,
    ApprovalStatus,
    ToolCallDecision,
    ToolInvocation,
    ToolExecutionMode,
    ToolInvocationStatus,
    args_hash_for,
)
from knuth.core.runs import AgentRun, Artifact
from knuth.core.runtime_events import (
    ApprovalRequestedDraft,
    ApprovalResolvedDraft,
    ModelAbortedDraft,
    ModelCompletedDraft,
    ModelFailedDraft,
    RunCancelledDraft,
    RunCreatedDraft,
    RunFailedDraft,
    RunPausedDraft,
    RunResumedDraft,
    RunSucceededDraft,
    StepStartedDraft,
    ToolBatchClosedDraft,
    ToolBatchPlannedDraft,
    ToolInvocationAwaitingExternalResultDraft,
    ToolInvocationCompletedDraft,
    ToolInvocationMarkedUnknownDraft,
    ToolInvocationStartedDraft,
    ToolProposedDraft,
    UserMessageDraft,
    VerificationFailedDraft,
)
from knuth.core.types import RunStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LedgerError(RuntimeError):
    """An event append violated an aggregate invariant."""


class EventRedactor(Protocol):
    """Redaction seam applied before append; the log is append-only, so
    plaintext that reaches it can never be unwritten."""

    def redact_event(self, draft: DurableRuntimeEventDraft) -> DurableRuntimeEventDraft:
        ...


@dataclass(frozen=True)
class OpenToolBatch:
    batch_id: str
    step_id: str
    invocations: tuple[ToolInvocation, ...]

    def by_status(self, *statuses: ToolInvocationStatus) -> tuple[ToolInvocation, ...]:
        wanted = set(statuses)
        return tuple(inv for inv in self.invocations if inv.status in wanted)


@dataclass(frozen=True)
class RunLedgerState:
    run: AgentRun
    open_batch: OpenToolBatch | None
    pending_approvals: tuple[Approval, ...]


@dataclass(frozen=True)
class RefoldStats:
    runs: int
    events: int


class RunLedger(Protocol):
    async def create_run(self, query: str, run_id: str | None = None) -> AgentRun:
        ...

    async def apply(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        ...

    async def get_run(self, run_id: str) -> AgentRun:
        ...

    async def list_runs(
        self, limit: int = 20, status: RunStatus | None = None
    ) -> list[AgentRun]:
        ...

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        ...

    async def run_state(self, run_id: str) -> RunLedgerState:
        ...

    async def pending_approvals(self, run_id: str | None = None) -> list[Approval]:
        ...

    async def get_approval(self, approval_id: str) -> Approval:
        ...

    async def get_invocation(self, tool_call_id: str) -> ToolInvocation:
        ...

    async def put_artifact(self, run_id: str, kind: str, content: str) -> Artifact:
        ...

    async def get_artifact_text(self, artifact_id: str) -> str:
        ...

    async def refold(self) -> RefoldStats:
        ...


_ACTIVE_STATUSES = frozenset({RunStatus.CREATED, RunStatus.RUNNING})
_FINISHED_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)


@dataclass
class _AggregateView:
    """Everything the reducer needs to validate one append, loaded in-transaction."""

    run: AgentRun | None
    invocations: dict[str, ToolInvocation] = field(default_factory=dict)
    approvals: dict[str, Approval] = field(default_factory=dict)
    last_model_tool_call_ids: tuple[str, ...] = ()


@dataclass
class _Mutations:
    run: AgentRun
    invocations: list[ToolInvocation] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LedgerError(message)


@dataclass
class _ReduceContext:
    """One append being validated: the aggregate view plus the mutations the
    reducer folds its changes into (``mutations.run`` is the run copy)."""

    view: _AggregateView
    mutations: _Mutations
    run_id: str
    seq: int
    now: str

    @property
    def run(self) -> AgentRun:
        return self.mutations.run

    def invocation(self, tool_call_id: str) -> ToolInvocation:
        invocation = self.view.invocations.get(tool_call_id)
        if invocation is None:
            raise LedgerError(f"unknown tool invocation: {tool_call_id}")
        return invocation


_Reducer = Callable[[_ReduceContext, Any], _Mutations]
_REDUCERS: dict[type[DurableRuntimeEventDraft], _Reducer] = {}


def _reduces(
    *draft_classes: type[DurableRuntimeEventDraft],
) -> Callable[[_Reducer], _Reducer]:
    def register(fn: _Reducer) -> _Reducer:
        for draft_cls in draft_classes:
            _REDUCERS[draft_cls] = fn
        return fn

    return register


def reduce_run_event(
    view: _AggregateView,
    draft: DurableRuntimeEventDraft,
    *,
    run_id: str,
    seq: int,
    now: str,
) -> _Mutations:
    """The aggregate: validates invariants and folds the event into projections.

    Each event type has its own reducer registered in ``_REDUCERS``. A raised
    ``LedgerError`` means the caller must not persist anything.
    """
    if isinstance(draft, RunCreatedDraft):
        _require(view.run is None, f"run already exists: {run_id}")
        return _Mutations(
            run=AgentRun(
                id=run_id,
                query=draft.query,
                status=RunStatus.CREATED,
                created_at=now,
                updated_at=now,
                last_seq=seq,
            )
        )

    if view.run is None:
        raise LedgerError(f"unknown run: {run_id}")
    # MRO lookup so stored events (draft subclasses) replay through the same
    # reducers during refold.
    reducer = next(
        (_REDUCERS[cls] for cls in type(draft).__mro__ if cls in _REDUCERS), None
    )
    if reducer is None:
        raise LedgerError(f"unsupported event type: {draft.type}")
    ctx = _ReduceContext(
        view=view,
        mutations=_Mutations(
            run=view.run.model_copy(update={"updated_at": now, "last_seq": seq})
        ),
        run_id=run_id,
        seq=seq,
        now=now,
    )
    return reducer(ctx, draft)


@_reduces(UserMessageDraft)
def _reduce_user_message(ctx: _ReduceContext, draft: UserMessageDraft) -> _Mutations:
    _require(
        ctx.run.status in {RunStatus.CREATED, RunStatus.SUCCEEDED},
        f"user.message requires a created or succeeded run, got {ctx.run.status}",
    )
    return ctx.mutations


@_reduces(RunResumedDraft)
def _reduce_run_resumed(ctx: _ReduceContext, draft: RunResumedDraft) -> _Mutations:
    _require(
        ctx.run.status
        in {
            RunStatus.RUNNING,
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_TOOL_RESULT,
            RunStatus.PAUSED,
            RunStatus.SUCCEEDED,
        },
        f"run.resumed not allowed from status {ctx.run.status}",
    )
    pending = [
        approval
        for approval in ctx.view.approvals.values()
        if approval.status == ApprovalStatus.PENDING
    ]
    _require(
        not pending,
        "pending approvals must be resolved before resuming: "
        + ", ".join(approval.id for approval in pending),
    )
    waiting_results = [
        invocation
        for invocation in ctx.view.invocations.values()
        if invocation.status == ToolInvocationStatus.WAITING_TOOL_RESULT
    ]
    _require(
        not waiting_results,
        "waiting tool results must be submitted before resuming: "
        + ", ".join(invocation.tool_call_id for invocation in waiting_results),
    )
    ctx.run.status = RunStatus.RUNNING
    return ctx.mutations


@_reduces(RunPausedDraft)
def _reduce_run_paused(ctx: _ReduceContext, draft: RunPausedDraft) -> _Mutations:
    _require(
        ctx.run.status in _ACTIVE_STATUSES,
        f"run.paused requires an active run, got {ctx.run.status}",
    )
    ctx.run.status = RunStatus.PAUSED
    return ctx.mutations


@_reduces(RunCancelledDraft)
def _reduce_run_cancelled(ctx: _ReduceContext, draft: RunCancelledDraft) -> _Mutations:
    _require(
        ctx.run.status not in _FINISHED_STATUSES,
        f"run.cancelled not allowed from status {ctx.run.status}",
    )
    ctx.run.status = RunStatus.CANCELLED
    return ctx.mutations


@_reduces(RunFailedDraft)
def _reduce_run_failed(ctx: _ReduceContext, draft: RunFailedDraft) -> _Mutations:
    _require(
        ctx.run.status not in _FINISHED_STATUSES,
        f"run.failed not allowed from status {ctx.run.status}",
    )
    ctx.run.status = RunStatus.FAILED
    return ctx.mutations


@_reduces(RunSucceededDraft)
def _reduce_run_succeeded(ctx: _ReduceContext, draft: RunSucceededDraft) -> _Mutations:
    _require(
        ctx.run.status == RunStatus.RUNNING,
        f"run.succeeded requires a running run, got {ctx.run.status}",
    )
    _require(ctx.run.open_batch_id is None, "run.succeeded requires no open tool batch")
    ctx.run.status = RunStatus.SUCCEEDED
    return ctx.mutations


@_reduces(StepStartedDraft)
def _reduce_step_started(ctx: _ReduceContext, draft: StepStartedDraft) -> _Mutations:
    _require(
        ctx.run.status in _ACTIVE_STATUSES,
        f"step.started requires an active run, got {ctx.run.status}",
    )
    _require(ctx.run.open_batch_id is None, "step.started requires no open tool batch")
    _require(
        draft.index == ctx.run.steps + 1,
        f"step index {draft.index} does not follow step count {ctx.run.steps}",
    )
    ctx.run.status = RunStatus.RUNNING
    ctx.run.steps += 1
    ctx.run.current_step_id = draft.step_id
    return ctx.mutations


@_reduces(ModelCompletedDraft)
def _reduce_model_completed(
    ctx: _ReduceContext, draft: ModelCompletedDraft
) -> _Mutations:
    _require(
        ctx.run.status == RunStatus.RUNNING, "model.completed requires a running run"
    )
    _require(
        draft.step_id == ctx.run.current_step_id,
        f"model.completed step {draft.step_id} is not the current step",
    )
    return ctx.mutations


@_reduces(ModelFailedDraft, ModelAbortedDraft)
def _reduce_model_outcome_noted(
    ctx: _ReduceContext, draft: ModelFailedDraft | ModelAbortedDraft
) -> _Mutations:
    return ctx.mutations

@_reduces(ToolBatchPlannedDraft)
def _reduce_tool_batch_planned(
    ctx: _ReduceContext, draft: ToolBatchPlannedDraft
) -> _Mutations:
    _require(
        ctx.run.status == RunStatus.RUNNING,
        "tool.batch_planned requires a running run",
    )
    _require(ctx.run.open_batch_id is None, "another tool batch is already open")
    _require(
        draft.step_id == ctx.run.current_step_id,
        f"tool.batch_planned step {draft.step_id} is not the current step",
    )
    _require(bool(draft.calls), "tool.batch_planned requires at least one call")
    planned_ids = {call.tool_call_id for call in draft.calls}
    _require(
        planned_ids == set(ctx.view.last_model_tool_call_ids),
        "planned calls do not match the latest model.completed tool calls",
    )
    for call in draft.calls:
        _require(
            call.args_hash == args_hash_for(call.args),
            f"args_hash mismatch for planned call {call.tool_call_id}",
        )
        ctx.mutations.invocations.append(
            ToolInvocation(
                tool_call_id=call.tool_call_id,
                run_id=ctx.run_id,
                batch_id=draft.batch_id,
                step_id=draft.step_id,
                index=call.index,
                tool_name=call.name,
                args=call.args,
                args_hash=call.args_hash,
                status=ToolInvocationStatus.PROPOSED,
                last_event_seq=ctx.seq,
            )
        )
    ctx.run.open_batch_id = draft.batch_id
    return ctx.mutations


@_reduces(ToolProposedDraft)
def _reduce_tool_proposed(ctx: _ReduceContext, draft: ToolProposedDraft) -> _Mutations:
    invocation = ctx.invocation(draft.tool_call_id)
    _require(
        invocation.batch_id == ctx.run.open_batch_id,
        "tool.proposed must target the open batch",
    )
    _require(
        invocation.status == ToolInvocationStatus.PROPOSED,
        f"tool.proposed requires status proposed, got {invocation.status}",
    )
    status_by_decision = {
        ToolCallDecision.ALLOWED: ToolInvocationStatus.APPROVED,
        ToolCallDecision.REQUIRES_APPROVAL: ToolInvocationStatus.AWAITING_APPROVAL,
        ToolCallDecision.DENIED: ToolInvocationStatus.DENIED,
    }
    updates: dict[str, Any] = {
        "status": status_by_decision[draft.decision],
        "effect": draft.effect,
        "risk": draft.risk,
        "execution_mode": draft.execution_mode,
        "last_event_seq": ctx.seq,
    }
    if draft.decision == ToolCallDecision.DENIED:
        updates["denied_observation"] = (
            draft.error.message if draft.error else "denied by policy"
        )
    ctx.mutations.invocations.append(invocation.model_copy(update=updates))
    return ctx.mutations


@_reduces(ApprovalRequestedDraft)
def _reduce_approval_requested(
    ctx: _ReduceContext, draft: ApprovalRequestedDraft
) -> _Mutations:
    invocation = ctx.invocation(draft.tool_call_id)
    _require(
        invocation.status == ToolInvocationStatus.AWAITING_APPROVAL,
        f"approval.requested requires awaiting_approval, got {invocation.status}",
    )
    _require(
        draft.args_hash == invocation.args_hash,
        "approval args_hash does not match the frozen invocation args",
    )
    existing = ctx.view.approvals.get(draft.approval_id)
    _require(
        existing is None or existing.status != ApprovalStatus.PENDING,
        f"approval already pending: {draft.approval_id}",
    )
    ctx.mutations.approvals.append(
        Approval(
            id=draft.approval_id,
            run_id=ctx.run_id,
            tool_call_id=draft.tool_call_id,
            args_hash=draft.args_hash,
            status=ApprovalStatus.PENDING,
            title=draft.title,
            reason=draft.reason,
            risk=draft.risk,
            approval_preview=draft.approval_preview,
            created_at=ctx.now,
        )
    )
    ctx.mutations.invocations.append(
        invocation.model_copy(
            update={"approval_id": draft.approval_id, "last_event_seq": ctx.seq}
        )
    )
    ctx.run.status = RunStatus.WAITING_APPROVAL
    return ctx.mutations


@_reduces(ApprovalResolvedDraft)
def _reduce_approval_resolved(
    ctx: _ReduceContext, draft: ApprovalResolvedDraft
) -> _Mutations:
    approval = ctx.view.approvals.get(draft.approval_id)
    if approval is None:
        raise LedgerError(f"unknown approval: {draft.approval_id}")
    _require(
        approval.status == ApprovalStatus.PENDING,
        f"approval already resolved: {draft.approval_id}",
    )
    approved = draft.resolution == "approved"
    ctx.mutations.approvals.append(
        approval.model_copy(
            update={
                "status": ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED,
                "resolved_at": ctx.now,
                "resolved_by": draft.resolved_by,
            }
        )
    )
    invocation = ctx.invocation(approval.tool_call_id)
    _require(
        invocation.status == ToolInvocationStatus.AWAITING_APPROVAL,
        f"approval target is not awaiting approval: {invocation.status}",
    )
    updates: dict[str, Any] = {"last_event_seq": ctx.seq}
    if approved:
        updates["status"] = ToolInvocationStatus.APPROVED
    else:
        updates["status"] = ToolInvocationStatus.DENIED
        updates["denied_observation"] = "denied by user"
    ctx.mutations.invocations.append(invocation.model_copy(update=updates))
    return ctx.mutations


@_reduces(ToolInvocationStartedDraft)
def _reduce_tool_invocation_started(
    ctx: _ReduceContext, draft: ToolInvocationStartedDraft
) -> _Mutations:
    _require(
        ctx.run.status == RunStatus.RUNNING,
        "tool.invocation_started requires a running run",
    )
    invocation = ctx.invocation(draft.tool_call_id)
    _require(
        invocation.status == ToolInvocationStatus.APPROVED,
        f"tool.invocation_started requires approved, got {invocation.status}",
    )
    _require(
        draft.attempt == invocation.attempt + 1,
        f"attempt {draft.attempt} does not follow attempt {invocation.attempt}",
    )
    ctx.mutations.invocations.append(
        invocation.model_copy(
            update={
                "status": ToolInvocationStatus.RUNNING,
                "attempt": invocation.attempt + 1,
                "last_event_seq": ctx.seq,
            }
        )
    )
    return ctx.mutations


@_reduces(ToolInvocationAwaitingExternalResultDraft)
def _reduce_tool_invocation_awaiting_external_result(
    ctx: _ReduceContext, draft: ToolInvocationAwaitingExternalResultDraft
) -> _Mutations:
    _require(
        ctx.run.status in {RunStatus.RUNNING, RunStatus.WAITING_TOOL_RESULT},
        "tool.invocation_awaiting_external_result requires a running or waiting run",
    )
    invocation = ctx.invocation(draft.tool_call_id)
    _require(
        invocation.status == ToolInvocationStatus.APPROVED,
        "tool.invocation_awaiting_external_result requires an approved invocation",
    )
    _require(
        invocation.execution_mode == ToolExecutionMode.EXTERNAL,
        "tool.invocation_awaiting_external_result requires an external invocation",
    )
    _require(
        invocation.tool_name == draft.tool_name,
        "tool.invocation_awaiting_external_result tool_name mismatch",
    )
    _require(
        invocation.args == draft.args,
        "tool.invocation_awaiting_external_result args mismatch",
    )
    ctx.mutations.invocations.append(
        invocation.model_copy(
            update={
                "status": ToolInvocationStatus.WAITING_TOOL_RESULT,
                "last_event_seq": ctx.seq,
            }
        )
    )
    ctx.run.status = RunStatus.WAITING_TOOL_RESULT
    return ctx.mutations


@_reduces(ToolInvocationCompletedDraft)
def _reduce_tool_invocation_completed(
    ctx: _ReduceContext, draft: ToolInvocationCompletedDraft
) -> _Mutations:
    invocation = ctx.invocation(draft.tool_call_id)
    if draft.outcome == "denied":
        _require(
            invocation.status == ToolInvocationStatus.DENIED
            and not invocation.observation_recorded,
            "denied observation backfill requires a denied invocation without observation",
        )
        new_status = ToolInvocationStatus.DENIED
    else:
        _require(
            invocation.status
            in {
                ToolInvocationStatus.RUNNING,
                ToolInvocationStatus.UNKNOWN,
                ToolInvocationStatus.WAITING_TOOL_RESULT,
            },
            "tool.invocation_completed requires a running, unknown, or waiting "
            "invocation, "
            f"got {invocation.status}",
        )
        new_status = (
            ToolInvocationStatus.SUCCEEDED
            if draft.outcome == "succeeded"
            else ToolInvocationStatus.FAILED
        )
    ctx.mutations.invocations.append(
        invocation.model_copy(
            update={
                "status": new_status,
                "observation_recorded": True,
                "last_event_seq": ctx.seq,
            }
        )
    )
    return ctx.mutations


@_reduces(ToolInvocationMarkedUnknownDraft)
def _reduce_tool_invocation_marked_unknown(
    ctx: _ReduceContext, draft: ToolInvocationMarkedUnknownDraft
) -> _Mutations:
    invocation = ctx.invocation(draft.tool_call_id)
    _require(
        invocation.status == ToolInvocationStatus.RUNNING,
        "only a running invocation can be marked unknown",
    )
    ctx.mutations.invocations.append(
        invocation.model_copy(
            update={"status": ToolInvocationStatus.UNKNOWN, "last_event_seq": ctx.seq}
        )
    )
    return ctx.mutations


@_reduces(ToolBatchClosedDraft)
def _reduce_tool_batch_closed(
    ctx: _ReduceContext, draft: ToolBatchClosedDraft
) -> _Mutations:
    _require(
        ctx.run.open_batch_id == draft.batch_id,
        f"tool.batch_closed batch {draft.batch_id} is not the open batch",
    )
    unobserved = [
        invocation.tool_call_id
        for invocation in ctx.view.invocations.values()
        if invocation.batch_id == draft.batch_id
        and not invocation.observation_recorded
    ]
    _require(
        not unobserved,
        "tool.batch_closed requires every call to have an observation; missing: "
        + ", ".join(unobserved),
    )
    ctx.run.open_batch_id = None
    return ctx.mutations


@_reduces(VerificationFailedDraft)
def _reduce_verification_failed(
    ctx: _ReduceContext, draft: VerificationFailedDraft
) -> _Mutations:
    _require(
        ctx.run.status == RunStatus.RUNNING,
        "verification.failed requires a running run",
    )
    _require(
        bool(draft.feedback.strip()),
        "verification.failed requires feedback; retrying without feedback is banned",
    )
    return ctx.mutations


def fold_stored_events(
    run_id: str, events: Iterable[StoredRuntimeEvent]
) -> _AggregateView:
    """Replay one run's durable events through the aggregate reducers.

    Projections are derived caches (design rule three): this fold is the
    canonical way to rebuild them, and doubles as a consistency check — a
    ``LedgerError`` here means the stored stream violates its own invariants.
    """
    view = _AggregateView(run=None)
    for event in events:
        mutations = reduce_run_event(
            view, event, run_id=run_id, seq=event.seq, now=event.created_at
        )
        view.run = mutations.run
        for invocation in mutations.invocations:
            view.invocations[invocation.tool_call_id] = invocation
        for approval in mutations.approvals:
            view.approvals[approval.id] = approval
        if event.type == "model.completed":
            view.last_model_tool_call_ids = tuple(
                call.effective_id for call in event.tool_calls
            )
    return view


def _open_batch_for(
    run: AgentRun, invocations: Iterable[ToolInvocation]
) -> OpenToolBatch | None:
    """Project the open ToolBatch from a run and its invocations."""
    if run.open_batch_id is None:
        return None
    batch_invocations = tuple(
        sorted(
            (inv for inv in invocations if inv.batch_id == run.open_batch_id),
            key=lambda inv: inv.index,
        )
    )
    return OpenToolBatch(
        batch_id=run.open_batch_id,
        step_id=batch_invocations[0].step_id if batch_invocations else "",
        invocations=batch_invocations,
    )


class _LedgerMixin:
    """Template for :class:`RunLedger` implementations.

    Owns the apply orchestration — redact, load the aggregate view, reduce,
    persist — so the event-sourcing flow exists exactly once. Subclasses
    supply storage and concurrency through ``_transact``, ``_load_view`` and
    ``_persist``.
    """

    _redactor: EventRedactor | None

    def __init__(self, redactor: EventRedactor | None = None) -> None:
        self._redactor = redactor

    async def apply(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        draft = self._redact(draft)
        return await self._transact(run_id, draft)

    async def create_run(self, query: str, run_id: str | None = None) -> AgentRun:
        run_id = run_id or f"run_{uuid4().hex}"
        await self.apply(run_id, RunCreatedDraft(query=query))
        return await self.get_run(run_id)

    def _redact(self, draft: DurableRuntimeEventDraft) -> DurableRuntimeEventDraft:
        if self._redactor is None:
            return draft
        return self._redactor.redact_event(draft)

    def _redact_artifact(self, content: str) -> str:
        """Artifacts referenced by events are semantically part of the ledger
        (design §1.2), so they get the same pre-write redaction when the
        redactor supports plain text."""
        redact_text = getattr(self._redactor, "redact_text", None)
        return redact_text(content) if redact_text is not None else content

    def _apply_in_txn(
        self, txn: Any, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        view = self._load_view(
            txn,
            run_id,
            # The reducer only consults the latest model tool-call ids when
            # validating a planned batch; skip the load otherwise.
            with_last_tool_call_ids=isinstance(draft, ToolBatchPlannedDraft),
        )
        seq = (view.run.last_seq if view.run else 0) + 1
        now = utc_now()
        mutations = reduce_run_event(view, draft, run_id=run_id, seq=seq, now=now)
        event = store_runtime_event(
            run_id, seq, draft, event_id=f"evt_{uuid4().hex}", created_at=now
        )
        self._persist(txn, run_id, event, mutations)
        return event

    async def _transact(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        raise NotImplementedError

    def _load_view(
        self, txn: Any, run_id: str, *, with_last_tool_call_ids: bool
    ) -> _AggregateView:
        raise NotImplementedError

    def _persist(
        self, txn: Any, run_id: str, event: StoredRuntimeEvent, mutations: _Mutations
    ) -> None:
        raise NotImplementedError

    async def get_run(self, run_id: str) -> AgentRun:
        raise NotImplementedError


class MemoryRunLedger(_LedgerMixin):
    def __init__(self, redactor: EventRedactor | None = None) -> None:
        super().__init__(redactor)
        self._lock = anyio.Lock()
        self._events: dict[str, list[StoredRuntimeEvent]] = {}
        self._runs: dict[str, AgentRun] = {}
        self._invocations: dict[str, dict[str, ToolInvocation]] = {}
        self._approvals: dict[str, Approval] = {}
        self._artifacts: dict[str, tuple[Artifact, str]] = {}

    async def _transact(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        async with self._lock:
            return self._apply_in_txn(None, run_id, draft)

    def _load_view(
        self, txn: Any, run_id: str, *, with_last_tool_call_ids: bool
    ) -> _AggregateView:
        return _AggregateView(
            run=self._runs.get(run_id),
            invocations=dict(self._invocations.get(run_id, {})),
            approvals={
                approval_id: approval
                for approval_id, approval in self._approvals.items()
                if approval.run_id == run_id
            },
            last_model_tool_call_ids=self._last_model_tool_call_ids(run_id)
            if with_last_tool_call_ids
            else (),
        )

    def _persist(
        self, txn: Any, run_id: str, event: StoredRuntimeEvent, mutations: _Mutations
    ) -> None:
        self._events.setdefault(run_id, []).append(event)
        self._runs[run_id] = mutations.run
        run_invocations = self._invocations.setdefault(run_id, {})
        for invocation in mutations.invocations:
            run_invocations[invocation.tool_call_id] = invocation
        for approval in mutations.approvals:
            self._approvals[approval.id] = approval

    def _last_model_tool_call_ids(self, run_id: str) -> tuple[str, ...]:
        for event in reversed(self._events.get(run_id, [])):
            if event.type == "model.completed":
                return tuple(
                    call.effective_id for call in event.tool_calls
                )
        return ()

    async def get_run(self, run_id: str) -> AgentRun:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    async def list_runs(
        self, limit: int = 20, status: RunStatus | None = None
    ) -> list[AgentRun]:
        runs = sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)
        if status is not None:
            runs = [run for run in runs if run.status == status]
        return runs[:limit]

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        events = self._events.get(run_id, [])
        if after_seq is None:
            return list(events)
        return [event for event in events if event.seq > after_seq]

    async def run_state(self, run_id: str) -> RunLedgerState:
        run = await self.get_run(run_id)
        pending = tuple(
            approval
            for approval in self._approvals.values()
            if approval.run_id == run_id and approval.status == ApprovalStatus.PENDING
        )
        return RunLedgerState(
            run=run,
            open_batch=_open_batch_for(run, self._invocations.get(run_id, {}).values()),
            pending_approvals=pending,
        )

    async def pending_approvals(self, run_id: str | None = None) -> list[Approval]:
        return [
            approval
            for approval in self._approvals.values()
            if approval.status == ApprovalStatus.PENDING
            and (run_id is None or approval.run_id == run_id)
        ]

    async def get_approval(self, approval_id: str) -> Approval:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        return approval

    async def get_invocation(self, tool_call_id: str) -> ToolInvocation:
        for invocations in self._invocations.values():
            if tool_call_id in invocations:
                return invocations[tool_call_id]
        raise KeyError(tool_call_id)

    async def put_artifact(self, run_id: str, kind: str, content: str) -> Artifact:
        content = self._redact_artifact(content)
        artifact = Artifact(
            id=f"art_{uuid4().hex}",
            run_id=run_id,
            kind=kind,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            created_at=utc_now(),
        )
        self._artifacts[artifact.id] = (artifact, content)
        return artifact

    async def get_artifact_text(self, artifact_id: str) -> str:
        if artifact_id not in self._artifacts:
            raise KeyError(artifact_id)
        return self._artifacts[artifact_id][1]

    async def refold(self) -> RefoldStats:
        async with self._lock:
            runs: dict[str, AgentRun] = {}
            invocations: dict[str, dict[str, ToolInvocation]] = {}
            approvals: dict[str, Approval] = {}
            total_events = 0
            for run_id, events in self._events.items():
                view = fold_stored_events(run_id, events)
                total_events += len(events)
                if view.run is None:
                    continue
                runs[run_id] = view.run
                invocations[run_id] = dict(view.invocations)
                approvals.update(view.approvals)
            self._runs = runs
            self._invocations = invocations
            self._approvals = approvals
            return RefoldStats(runs=len(runs), events=total_events)


def _threaded[T, **P](fn: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    """Lift a blocking method onto a worker thread, keeping the async API."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


class SQLiteRunLedger(_LedgerMixin):
    def __init__(
        self, db_path: Path | str, redactor: EventRedactor | None = None
    ) -> None:
        super().__init__(redactor)
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists runs (
                  id text primary key,
                  status text not null,
                  query text not null,
                  steps integer not null default 0,
                  open_batch_id text,
                  current_step_id text,
                  last_seq integer not null default 0,
                  created_at text not null,
                  updated_at text not null,
                  data_json text not null
                );
                create table if not exists events (
                  id text primary key,
                  run_id text not null,
                  seq integer not null,
                  type text not null,
                  step_id text,
                  event_json text not null,
                  created_at text not null,
                  unique(run_id, seq)
                );
                create table if not exists tool_invocations (
                  tool_call_id text primary key,
                  run_id text not null,
                  batch_id text not null,
                  step_id text not null,
                  idx integer not null,
                  tool_name text not null,
                  status text not null,
                  observation_recorded integer not null default 0,
                  data_json text not null,
                  last_event_seq integer not null
                );
                create table if not exists approvals (
                  id text primary key,
                  run_id text not null,
                  tool_call_id text not null,
                  status text not null,
                  data_json text not null,
                  created_at text not null,
                  resolved_at text
                );
                create table if not exists artifacts (
                  id text primary key,
                  run_id text not null,
                  kind text not null,
                  sha256 text not null,
                  content text not null,
                  created_at text not null
                );
                """
            )
            self._guard_schema(conn)

    def _guard_schema(self, conn) -> None:
        run_columns = {row[1] for row in conn.execute("pragma table_info(runs)")}
        event_columns = {row[1] for row in conn.execute("pragma table_info(events)")}
        invocation_columns = {
            row[1] for row in conn.execute("pragma table_info(tool_invocations)")
        }
        approval_columns = {
            row[1] for row in conn.execute("pragma table_info(approvals)")
        }
        if (
            "last_seq" not in run_columns
            or "step_id" not in event_columns
            or "observation_recorded" not in invocation_columns
            or "last_event_seq" not in invocation_columns
            or "tool_call_id" not in approval_columns
        ):
            raise RuntimeError(
                "breaking ledger schema: remove the legacy database or use a new one"
            )

    @_threaded
    def _transact(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        with self._connect() as conn:
            return self._apply_in_txn(conn, run_id, draft)

    def _load_view(
        self, txn: sqlite3.Connection, run_id: str, *, with_last_tool_call_ids: bool
    ) -> _AggregateView:
        invocations = self._select(
            txn,
            ToolInvocation,
            "select data_json from tool_invocations where run_id = ?",
            (run_id,),
        )
        approvals = self._select(
            txn,
            Approval,
            "select data_json from approvals where run_id = ?",
            (run_id,),
        )
        return _AggregateView(
            run=self._load_run(txn, run_id),
            invocations={inv.tool_call_id: inv for inv in invocations},
            approvals={approval.id: approval for approval in approvals},
            last_model_tool_call_ids=self._load_last_model_tool_call_ids(txn, run_id)
            if with_last_tool_call_ids
            else (),
        )

    def _persist(
        self,
        txn: sqlite3.Connection,
        run_id: str,
        event: StoredRuntimeEvent,
        mutations: _Mutations,
    ) -> None:
        txn.execute(
            "insert into events (id, run_id, seq, type, step_id, event_json, created_at)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                run_id,
                event.seq,
                event.type,
                getattr(event, "step_id", None),
                event.model_dump_json(),
                event.created_at,
            ),
        )
        self._upsert_run(txn, mutations.run)
        for invocation in mutations.invocations:
            self._upsert_invocation(txn, invocation)
        for approval in mutations.approvals:
            self._upsert_approval(txn, approval)

    @staticmethod
    def _select[M: BaseModel](
        conn: sqlite3.Connection,
        model_cls: type[M],
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> list[M]:
        return [
            model_cls.model_validate_json(row[0]) for row in conn.execute(sql, params)
        ]

    def _load_run(self, conn: sqlite3.Connection, run_id: str) -> AgentRun | None:
        row = conn.execute(
            "select data_json from runs where id = ?", (run_id,)
        ).fetchone()
        return AgentRun.model_validate_json(row[0]) if row else None

    def _load_last_model_tool_call_ids(self, conn, run_id: str) -> tuple[str, ...]:
        row = conn.execute(
            "select event_json from events where run_id = ? and type = 'model.completed'"
            " order by seq desc limit 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return ()
        event = parse_stored_runtime_event_json(row[0])
        return tuple(call.effective_id for call in event.tool_calls)

    def _upsert_run(self, conn, run: AgentRun) -> None:
        conn.execute(
            "insert into runs"
            " (id, status, query, steps, open_batch_id, current_step_id, last_seq,"
            "  created_at, updated_at, data_json)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " on conflict(id) do update set"
            " status=excluded.status, steps=excluded.steps,"
            " open_batch_id=excluded.open_batch_id,"
            " current_step_id=excluded.current_step_id, last_seq=excluded.last_seq,"
            " updated_at=excluded.updated_at, data_json=excluded.data_json",
            (
                run.id,
                run.status.value,
                run.query,
                run.steps,
                run.open_batch_id,
                run.current_step_id,
                run.last_seq,
                run.created_at,
                run.updated_at,
                run.model_dump_json(),
            ),
        )

    def _upsert_invocation(self, conn, invocation: ToolInvocation) -> None:
        conn.execute(
            "insert into tool_invocations"
            " (tool_call_id, run_id, batch_id, step_id, idx, tool_name, status,"
            "  observation_recorded, data_json, last_event_seq)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " on conflict(tool_call_id) do update set"
            " status=excluded.status,"
            " observation_recorded=excluded.observation_recorded,"
            " data_json=excluded.data_json, last_event_seq=excluded.last_event_seq",
            (
                invocation.tool_call_id,
                invocation.run_id,
                invocation.batch_id,
                invocation.step_id,
                invocation.index,
                invocation.tool_name,
                invocation.status.value,
                1 if invocation.observation_recorded else 0,
                invocation.model_dump_json(),
                invocation.last_event_seq,
            ),
        )

    def _upsert_approval(self, conn, approval: Approval) -> None:
        conn.execute(
            "insert into approvals"
            " (id, run_id, tool_call_id, status, data_json, created_at, resolved_at)"
            " values (?, ?, ?, ?, ?, ?, ?)"
            " on conflict(id) do update set"
            " status=excluded.status, data_json=excluded.data_json,"
            " resolved_at=excluded.resolved_at",
            (
                approval.id,
                approval.run_id,
                approval.tool_call_id,
                approval.status.value,
                approval.model_dump_json(),
                approval.created_at,
                approval.resolved_at,
            ),
        )

    @_threaded
    def get_run(self, run_id: str) -> AgentRun:
        with self._connect() as conn:
            run = self._load_run(conn, run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    @_threaded
    def list_runs(
        self, limit: int = 20, status: RunStatus | None = None
    ) -> list[AgentRun]:
        sql = "select data_json from runs"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " where status = ?"
            params += (status.value,)
        sql += " order by created_at desc limit ?"
        params += (limit,)
        with self._connect() as conn:
            return self._select(conn, AgentRun, sql, params)

    @_threaded
    def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        sql = "select event_json from events where run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if after_seq is not None:
            sql += " and seq > ?"
            params += (after_seq,)
        sql += " order by seq"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [parse_stored_runtime_event_json(row[0]) for row in rows]

    @_threaded
    def run_state(self, run_id: str) -> RunLedgerState:
        with self._connect() as conn:
            run = self._load_run(conn, run_id)
            if run is None:
                raise KeyError(run_id)
            invocations: list[ToolInvocation] = []
            if run.open_batch_id is not None:
                invocations = self._select(
                    conn,
                    ToolInvocation,
                    "select data_json from tool_invocations"
                    " where run_id = ? and batch_id = ?",
                    (run_id, run.open_batch_id),
                )
            pending = tuple(
                self._select(
                    conn,
                    Approval,
                    "select data_json from approvals where run_id = ? and status = ?",
                    (run_id, ApprovalStatus.PENDING.value),
                )
            )
        return RunLedgerState(
            run=run,
            open_batch=_open_batch_for(run, invocations),
            pending_approvals=pending,
        )

    @_threaded
    def pending_approvals(self, run_id: str | None = None) -> list[Approval]:
        sql = "select data_json from approvals where status = ?"
        params: tuple[Any, ...] = (ApprovalStatus.PENDING.value,)
        if run_id is not None:
            sql += " and run_id = ?"
            params += (run_id,)
        with self._connect() as conn:
            return self._select(conn, Approval, sql, params)

    @_threaded
    def get_approval(self, approval_id: str) -> Approval:
        with self._connect() as conn:
            row = conn.execute(
                "select data_json from approvals where id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return Approval.model_validate_json(row[0])

    @_threaded
    def get_invocation(self, tool_call_id: str) -> ToolInvocation:
        with self._connect() as conn:
            row = conn.execute(
                "select data_json from tool_invocations where tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
        if row is None:
            raise KeyError(tool_call_id)
        return ToolInvocation.model_validate_json(row[0])

    @_threaded
    def put_artifact(self, run_id: str, kind: str, content: str) -> Artifact:
        content = self._redact_artifact(content)
        artifact = Artifact(
            id=f"art_{uuid4().hex}",
            run_id=run_id,
            kind=kind,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            created_at=utc_now(),
        )
        with self._connect() as conn:
            conn.execute(
                "insert into artifacts (id, run_id, kind, sha256, content, created_at)"
                " values (?, ?, ?, ?, ?, ?)",
                (
                    artifact.id,
                    artifact.run_id,
                    artifact.kind,
                    artifact.sha256,
                    content,
                    artifact.created_at,
                ),
            )
        return artifact

    @_threaded
    def get_artifact_text(self, artifact_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "select content from artifacts where id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return row[0]

    @_threaded
    def refold(self) -> RefoldStats:
        # One transaction: a failed replay rolls back and leaves the existing
        # projections untouched.
        with self._connect() as conn:
            run_ids = [
                row[0]
                for row in conn.execute("select distinct run_id from events")
            ]
            conn.execute("delete from runs")
            conn.execute("delete from tool_invocations")
            conn.execute("delete from approvals")
            total_events = 0
            for run_id in run_ids:
                events = self._load_events(conn, run_id)
                view = fold_stored_events(run_id, events)
                total_events += len(events)
                if view.run is None:
                    continue
                self._upsert_run(conn, view.run)
                for invocation in view.invocations.values():
                    self._upsert_invocation(conn, invocation)
                for approval in view.approvals.values():
                    self._upsert_approval(conn, approval)
        return RefoldStats(runs=len(run_ids), events=total_events)

    def _load_events(
        self, conn: sqlite3.Connection, run_id: str
    ) -> list[StoredRuntimeEvent]:
        rows = conn.execute(
            "select event_json from events where run_id = ? order by seq",
            (run_id,),
        ).fetchall()
        return [parse_stored_runtime_event_json(row[0]) for row in rows]
