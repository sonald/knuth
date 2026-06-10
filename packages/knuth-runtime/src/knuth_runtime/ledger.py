from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import anyio

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
    ToolInvocationStatus,
    args_hash_for,
)
from knuth.core.runs import AgentRun, Artifact
from knuth.core.runtime_events import (
    ApprovalRequestedDraft,
    ApprovalResolvedDraft,
    ContextCompactedDraft,
    ModelCompletedDraft,
    RunCheckpointDraft,
    RunCreatedDraft,
    RunPausedDraft,
    RunResumedDraft,
    RunSucceededDraft,
    StepStartedDraft,
    ToolBatchClosedDraft,
    ToolBatchPlannedDraft,
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


class RunLedger(Protocol):
    async def create_run(
        self, query: str, metadata: dict[str, Any] | None = None
    ) -> AgentRun:
        ...

    async def apply(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        ...

    async def get_run(self, run_id: str) -> AgentRun:
        ...

    async def list_runs(self, limit: int = 20) -> list[AgentRun]:
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


def _invocation(view: _AggregateView, tool_call_id: str) -> ToolInvocation:
    invocation = view.invocations.get(tool_call_id)
    _require(invocation is not None, f"unknown tool invocation: {tool_call_id}")
    assert invocation is not None
    return invocation


def reduce_run_event(
    view: _AggregateView,
    draft: DurableRuntimeEventDraft,
    *,
    run_id: str,
    seq: int,
    now: str,
) -> _Mutations:
    """The aggregate: validates invariants and folds the event into projections.

    Raises ``LedgerError`` on violation; the caller must not persist anything
    in that case.
    """
    if isinstance(draft, (ContextCompactedDraft, RunCheckpointDraft)):
        raise LedgerError(f"reserved event type not implemented in v0: {draft.type}")

    if isinstance(draft, RunCreatedDraft):
        _require(view.run is None, f"run already exists: {run_id}")
        run = AgentRun(
            id=run_id,
            query=draft.query,
            status=RunStatus.CREATED,
            created_at=now,
            updated_at=now,
            metadata=draft.metadata,
            last_seq=seq,
        )
        return _Mutations(run=run)

    _require(view.run is not None, f"unknown run: {run_id}")
    assert view.run is not None
    run = view.run.model_copy(update={"updated_at": now, "last_seq": seq})
    status = run.status
    mutations = _Mutations(run=run)

    if isinstance(draft, UserMessageDraft):
        _require(
            status in {RunStatus.CREATED, RunStatus.SUCCEEDED},
            f"user.message requires a created or succeeded run, got {status}",
        )
        return mutations

    if isinstance(draft, RunResumedDraft):
        _require(
            status
            in {
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.PAUSED,
                RunStatus.SUCCEEDED,
            },
            f"run.resumed not allowed from status {status}",
        )
        pending = [
            approval
            for approval in view.approvals.values()
            if approval.status == ApprovalStatus.PENDING
        ]
        _require(
            not pending,
            "pending approvals must be resolved before resuming: "
            + ", ".join(approval.id for approval in pending),
        )
        run.status = RunStatus.RUNNING
        return mutations

    if isinstance(draft, RunPausedDraft):
        _require(
            status in _ACTIVE_STATUSES,
            f"run.paused requires an active run, got {status}",
        )
        run.status = RunStatus.PAUSED
        return mutations

    if draft.type == "run.cancelled":
        _require(
            status not in _FINISHED_STATUSES,
            f"run.cancelled not allowed from status {status}",
        )
        run.status = RunStatus.CANCELLED
        return mutations

    if draft.type == "run.failed":
        _require(
            status not in _FINISHED_STATUSES,
            f"run.failed not allowed from status {status}",
        )
        run.status = RunStatus.FAILED
        return mutations

    if isinstance(draft, RunSucceededDraft):
        _require(
            status == RunStatus.RUNNING,
            f"run.succeeded requires a running run, got {status}",
        )
        _require(run.open_batch_id is None, "run.succeeded requires no open tool batch")
        run.status = RunStatus.SUCCEEDED
        return mutations

    if isinstance(draft, StepStartedDraft):
        _require(
            status in _ACTIVE_STATUSES,
            f"step.started requires an active run, got {status}",
        )
        _require(run.open_batch_id is None, "step.started requires no open tool batch")
        _require(
            draft.index == run.steps + 1,
            f"step index {draft.index} does not follow step count {run.steps}",
        )
        run.status = RunStatus.RUNNING
        run.steps += 1
        run.current_step_id = draft.step_id
        return mutations

    if isinstance(draft, ModelCompletedDraft):
        _require(status == RunStatus.RUNNING, "model.completed requires a running run")
        _require(
            draft.step_id == run.current_step_id,
            f"model.completed step {draft.step_id} is not the current step",
        )
        return mutations

    if draft.type in {"model.failed", "model.aborted"}:
        return mutations

    if isinstance(draft, ToolBatchPlannedDraft):
        _require(status == RunStatus.RUNNING, "tool.batch_planned requires a running run")
        _require(run.open_batch_id is None, "another tool batch is already open")
        _require(
            draft.step_id == run.current_step_id,
            f"tool.batch_planned step {draft.step_id} is not the current step",
        )
        _require(bool(draft.calls), "tool.batch_planned requires at least one call")
        planned_ids = {call.tool_call_id for call in draft.calls}
        _require(
            planned_ids == set(view.last_model_tool_call_ids),
            "planned calls do not match the latest model.completed tool calls",
        )
        for call in draft.calls:
            _require(
                call.args_hash == args_hash_for(call.args),
                f"args_hash mismatch for planned call {call.tool_call_id}",
            )
            mutations.invocations.append(
                ToolInvocation(
                    tool_call_id=call.tool_call_id,
                    run_id=run_id,
                    batch_id=draft.batch_id,
                    step_id=draft.step_id,
                    index=call.index,
                    tool_name=call.name,
                    args=call.args,
                    args_hash=call.args_hash,
                    status=ToolInvocationStatus.PROPOSED,
                    updated_seq=seq,
                )
            )
        run.open_batch_id = draft.batch_id
        return mutations

    if isinstance(draft, ToolProposedDraft):
        invocation = _invocation(view, draft.tool_call_id)
        _require(
            invocation.batch_id == run.open_batch_id,
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
            "updated_seq": seq,
        }
        if draft.decision == ToolCallDecision.DENIED:
            updates["denial_reason"] = (
                draft.error.message if draft.error else "denied by policy"
            )
        mutations.invocations.append(invocation.model_copy(update=updates))
        return mutations

    if isinstance(draft, ApprovalRequestedDraft):
        invocation = _invocation(view, draft.tool_call_id)
        _require(
            invocation.status == ToolInvocationStatus.AWAITING_APPROVAL,
            f"approval.requested requires awaiting_approval, got {invocation.status}",
        )
        _require(
            draft.args_hash == invocation.args_hash,
            "approval args_hash does not match the frozen invocation args",
        )
        existing = view.approvals.get(draft.approval_id)
        _require(
            existing is None or existing.status != ApprovalStatus.PENDING,
            f"approval already pending: {draft.approval_id}",
        )
        mutations.approvals.append(
            Approval(
                id=draft.approval_id,
                run_id=run_id,
                tool_call_id=draft.tool_call_id,
                args_hash=draft.args_hash,
                status=ApprovalStatus.PENDING,
                title=draft.title,
                reason=draft.reason,
                risk=draft.risk,
                preview=draft.preview,
                created_at=now,
            )
        )
        mutations.invocations.append(
            invocation.model_copy(
                update={"approval_id": draft.approval_id, "updated_seq": seq}
            )
        )
        run.status = RunStatus.WAITING_APPROVAL
        return mutations

    if isinstance(draft, ApprovalResolvedDraft):
        approval = view.approvals.get(draft.approval_id)
        _require(approval is not None, f"unknown approval: {draft.approval_id}")
        assert approval is not None
        _require(
            approval.status == ApprovalStatus.PENDING,
            f"approval already resolved: {draft.approval_id}",
        )
        resolved_status = (
            ApprovalStatus.APPROVED
            if draft.resolution == "approved"
            else ApprovalStatus.DENIED
        )
        mutations.approvals.append(
            approval.model_copy(
                update={
                    "status": resolved_status,
                    "resolved_at": now,
                    "resolved_by": draft.resolved_by,
                }
            )
        )
        invocation = _invocation(view, approval.tool_call_id)
        _require(
            invocation.status == ToolInvocationStatus.AWAITING_APPROVAL,
            f"approval target is not awaiting approval: {invocation.status}",
        )
        updates: dict[str, Any] = {"updated_seq": seq}
        if draft.resolution == "approved":
            updates["status"] = ToolInvocationStatus.APPROVED
        else:
            updates["status"] = ToolInvocationStatus.DENIED
            updates["denial_reason"] = "denied by user"
        mutations.invocations.append(invocation.model_copy(update=updates))
        return mutations

    if isinstance(draft, ToolInvocationStartedDraft):
        _require(
            status == RunStatus.RUNNING,
            "tool.invocation_started requires a running run",
        )
        invocation = _invocation(view, draft.tool_call_id)
        _require(
            invocation.status == ToolInvocationStatus.APPROVED,
            f"tool.invocation_started requires approved, got {invocation.status}",
        )
        _require(
            draft.attempt == invocation.attempts + 1,
            f"attempt {draft.attempt} does not follow attempts {invocation.attempts}",
        )
        mutations.invocations.append(
            invocation.model_copy(
                update={
                    "status": ToolInvocationStatus.RUNNING,
                    "attempts": invocation.attempts + 1,
                    "idempotency_key": draft.idempotency_key,
                    "updated_seq": seq,
                }
            )
        )
        return mutations

    if isinstance(draft, ToolInvocationCompletedDraft):
        invocation = _invocation(view, draft.tool_call_id)
        if draft.outcome == "denied":
            _require(
                invocation.status == ToolInvocationStatus.DENIED
                and not invocation.observed,
                "denied observation backfill requires an unobserved denied invocation",
            )
            new_status = ToolInvocationStatus.DENIED
        else:
            _require(
                invocation.status
                in {ToolInvocationStatus.RUNNING, ToolInvocationStatus.UNKNOWN},
                "tool.invocation_completed requires a running or unknown invocation, "
                f"got {invocation.status}",
            )
            new_status = (
                ToolInvocationStatus.SUCCEEDED
                if draft.outcome == "succeeded"
                else ToolInvocationStatus.FAILED
            )
        mutations.invocations.append(
            invocation.model_copy(
                update={
                    "status": new_status,
                    "observed": True,
                    "updated_seq": seq,
                }
            )
        )
        return mutations

    if isinstance(draft, ToolInvocationMarkedUnknownDraft):
        invocation = _invocation(view, draft.tool_call_id)
        _require(
            invocation.status == ToolInvocationStatus.RUNNING,
            "only a running invocation can be marked unknown",
        )
        mutations.invocations.append(
            invocation.model_copy(
                update={"status": ToolInvocationStatus.UNKNOWN, "updated_seq": seq}
            )
        )
        return mutations

    if isinstance(draft, ToolBatchClosedDraft):
        _require(
            run.open_batch_id == draft.batch_id,
            f"tool.batch_closed batch {draft.batch_id} is not the open batch",
        )
        unobserved = [
            invocation.tool_call_id
            for invocation in view.invocations.values()
            if invocation.batch_id == draft.batch_id and not invocation.observed
        ]
        _require(
            not unobserved,
            "tool.batch_closed requires every call to have an observation; missing: "
            + ", ".join(unobserved),
        )
        run.open_batch_id = None
        return mutations

    if isinstance(draft, VerificationFailedDraft):
        _require(status == RunStatus.RUNNING, "verification.failed requires a running run")
        _require(
            bool(draft.feedback.strip()),
            "verification.failed requires feedback; retrying without feedback is banned",
        )
        return mutations

    raise LedgerError(f"unsupported event type: {draft.type}")


class _BaseRunLedger:
    """Shared apply/read shape over an implementation-specific transaction."""

    def __init__(self, redactor: EventRedactor | None = None) -> None:
        self._redactor = redactor

    def _redact(self, draft: DurableRuntimeEventDraft) -> DurableRuntimeEventDraft:
        if self._redactor is None:
            return draft
        return self._redactor.redact_event(draft)

    async def create_run(
        self, query: str, metadata: dict[str, Any] | None = None
    ) -> AgentRun:
        run_id = f"run_{uuid4().hex}"
        await self.apply(run_id, RunCreatedDraft(query=query, metadata=metadata or {}))
        return await self.get_run(run_id)

    async def apply(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        raise NotImplementedError

    async def get_run(self, run_id: str) -> AgentRun:
        raise NotImplementedError


class MemoryRunLedger(_BaseRunLedger):
    def __init__(self, redactor: EventRedactor | None = None) -> None:
        super().__init__(redactor)
        self._lock = anyio.Lock()
        self._events: dict[str, list[StoredRuntimeEvent]] = {}
        self._runs: dict[str, AgentRun] = {}
        self._invocations: dict[str, dict[str, ToolInvocation]] = {}
        self._approvals: dict[str, Approval] = {}
        self._artifacts: dict[str, tuple[Artifact, str]] = {}

    async def apply(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        draft = self._redact(draft)
        async with self._lock:
            run = self._runs.get(run_id)
            seq = (run.last_seq if run else 0) + 1
            view = _AggregateView(
                run=run,
                invocations=dict(self._invocations.get(run_id, {})),
                approvals={
                    approval_id: approval
                    for approval_id, approval in self._approvals.items()
                    if approval.run_id == run_id
                },
                last_model_tool_call_ids=self._last_model_tool_call_ids(run_id),
            )
            now = utc_now()
            mutations = reduce_run_event(view, draft, run_id=run_id, seq=seq, now=now)
            event = store_runtime_event(
                run_id,
                seq,
                draft,
                event_id=f"evt_{uuid4().hex}",
                created_at=now,
            )
            self._events.setdefault(run_id, []).append(event)
            self._runs[run_id] = mutations.run
            for invocation in mutations.invocations:
                self._invocations.setdefault(run_id, {})[
                    invocation.tool_call_id
                ] = invocation
            for approval in mutations.approvals:
                self._approvals[approval.id] = approval
            return event

    def _last_model_tool_call_ids(self, run_id: str) -> tuple[str, ...]:
        for event in reversed(self._events.get(run_id, [])):
            if event.type == "model.completed":
                return tuple(
                    call.id or f"call_{call.index}" for call in event.tool_calls
                )
        return ()

    async def get_run(self, run_id: str) -> AgentRun:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    async def list_runs(self, limit: int = 20) -> list[AgentRun]:
        runs = sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)
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
        open_batch = None
        if run.open_batch_id is not None:
            invocations = tuple(
                sorted(
                    (
                        invocation
                        for invocation in self._invocations.get(run_id, {}).values()
                        if invocation.batch_id == run.open_batch_id
                    ),
                    key=lambda invocation: invocation.index,
                )
            )
            step_id = invocations[0].step_id if invocations else ""
            open_batch = OpenToolBatch(
                batch_id=run.open_batch_id,
                step_id=step_id,
                invocations=invocations,
            )
        pending = tuple(
            approval
            for approval in self._approvals.values()
            if approval.run_id == run_id and approval.status == ApprovalStatus.PENDING
        )
        return RunLedgerState(run=run, open_batch=open_batch, pending_approvals=pending)

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


class SQLiteRunLedger(_BaseRunLedger):
    def __init__(
        self, db_path: Path | str, redactor: EventRedactor | None = None
    ) -> None:
        super().__init__(redactor)
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        import sqlite3

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
                  observed integer not null default 0,
                  data_json text not null,
                  updated_seq integer not null
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
        approval_columns = {
            row[1] for row in conn.execute("pragma table_info(approvals)")
        }
        if (
            "last_seq" not in run_columns
            or "step_id" not in event_columns
            or "tool_call_id" not in approval_columns
        ):
            raise RuntimeError(
                "breaking ledger schema: remove the legacy database or use a new one"
            )

    async def apply(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        draft = self._redact(draft)
        return await anyio.to_thread.run_sync(self._apply, run_id, draft)

    def _apply(
        self, run_id: str, draft: DurableRuntimeEventDraft
    ) -> StoredRuntimeEvent:
        with self._connect() as conn:
            run = self._load_run(conn, run_id)
            seq = (run.last_seq if run else 0) + 1
            view = _AggregateView(
                run=run,
                invocations=self._load_invocations(conn, run_id),
                approvals=self._load_approvals(conn, run_id),
                last_model_tool_call_ids=self._load_last_model_tool_call_ids(
                    conn, run_id
                )
                if isinstance(draft, ToolBatchPlannedDraft)
                else (),
            )
            now = utc_now()
            mutations = reduce_run_event(view, draft, run_id=run_id, seq=seq, now=now)
            event = store_runtime_event(
                run_id,
                seq,
                draft,
                event_id=f"evt_{uuid4().hex}",
                created_at=now,
            )
            conn.execute(
                "insert into events (id, run_id, seq, type, step_id, event_json, created_at)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    run_id,
                    seq,
                    event.type,
                    getattr(event, "step_id", None),
                    event.model_dump_json(),
                    now,
                ),
            )
            self._upsert_run(conn, mutations.run)
            for invocation in mutations.invocations:
                self._upsert_invocation(conn, invocation)
            for approval in mutations.approvals:
                self._upsert_approval(conn, approval)
            return event

    def _load_run(self, conn, run_id: str) -> AgentRun | None:
        row = conn.execute(
            "select data_json from runs where id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return AgentRun.model_validate_json(row[0])

    def _load_invocations(self, conn, run_id: str) -> dict[str, ToolInvocation]:
        rows = conn.execute(
            "select data_json from tool_invocations where run_id = ?", (run_id,)
        ).fetchall()
        invocations = [ToolInvocation.model_validate_json(row[0]) for row in rows]
        return {invocation.tool_call_id: invocation for invocation in invocations}

    def _load_approvals(self, conn, run_id: str) -> dict[str, Approval]:
        rows = conn.execute(
            "select data_json from approvals where run_id = ?", (run_id,)
        ).fetchall()
        approvals = [Approval.model_validate_json(row[0]) for row in rows]
        return {approval.id: approval for approval in approvals}

    def _load_last_model_tool_call_ids(self, conn, run_id: str) -> tuple[str, ...]:
        row = conn.execute(
            "select event_json from events where run_id = ? and type = 'model.completed'"
            " order by seq desc limit 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return ()
        event = parse_stored_runtime_event_json(row[0])
        return tuple(call.id or f"call_{call.index}" for call in event.tool_calls)

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
            "  observed, data_json, updated_seq)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " on conflict(tool_call_id) do update set"
            " status=excluded.status, observed=excluded.observed,"
            " data_json=excluded.data_json, updated_seq=excluded.updated_seq",
            (
                invocation.tool_call_id,
                invocation.run_id,
                invocation.batch_id,
                invocation.step_id,
                invocation.index,
                invocation.tool_name,
                invocation.status.value,
                1 if invocation.observed else 0,
                invocation.model_dump_json(),
                invocation.updated_seq,
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

    async def get_run(self, run_id: str) -> AgentRun:
        run = await anyio.to_thread.run_sync(self._get_run, run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def _get_run(self, run_id: str) -> AgentRun | None:
        with self._connect() as conn:
            return self._load_run(conn, run_id)

    async def list_runs(self, limit: int = 20) -> list[AgentRun]:
        return await anyio.to_thread.run_sync(self._list_runs, limit)

    def _list_runs(self, limit: int) -> list[AgentRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "select data_json from runs order by created_at desc limit ?",
                (limit,),
            ).fetchall()
        return [AgentRun.model_validate_json(row[0]) for row in rows]

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        return await anyio.to_thread.run_sync(self._list_events, run_id, after_seq)

    def _list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        sql = "select event_json from events where run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if after_seq is not None:
            sql += " and seq > ?"
            params = (run_id, after_seq)
        sql += " order by seq"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [parse_stored_runtime_event_json(row[0]) for row in rows]

    async def run_state(self, run_id: str) -> RunLedgerState:
        return await anyio.to_thread.run_sync(self._run_state, run_id)

    def _run_state(self, run_id: str) -> RunLedgerState:
        with self._connect() as conn:
            run = self._load_run(conn, run_id)
            if run is None:
                raise KeyError(run_id)
            open_batch = None
            if run.open_batch_id is not None:
                rows = conn.execute(
                    "select data_json from tool_invocations"
                    " where run_id = ? and batch_id = ? order by idx",
                    (run_id, run.open_batch_id),
                ).fetchall()
                invocations = tuple(
                    ToolInvocation.model_validate_json(row[0]) for row in rows
                )
                step_id = invocations[0].step_id if invocations else ""
                open_batch = OpenToolBatch(
                    batch_id=run.open_batch_id,
                    step_id=step_id,
                    invocations=invocations,
                )
            pending_rows = conn.execute(
                "select data_json from approvals where run_id = ? and status = ?",
                (run_id, ApprovalStatus.PENDING.value),
            ).fetchall()
            pending = tuple(
                Approval.model_validate_json(row[0]) for row in pending_rows
            )
        return RunLedgerState(run=run, open_batch=open_batch, pending_approvals=pending)

    async def pending_approvals(self, run_id: str | None = None) -> list[Approval]:
        return await anyio.to_thread.run_sync(self._pending_approvals, run_id)

    def _pending_approvals(self, run_id: str | None) -> list[Approval]:
        sql = "select data_json from approvals where status = ?"
        params: tuple[Any, ...] = (ApprovalStatus.PENDING.value,)
        if run_id is not None:
            sql += " and run_id = ?"
            params = (ApprovalStatus.PENDING.value, run_id)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Approval.model_validate_json(row[0]) for row in rows]

    async def get_approval(self, approval_id: str) -> Approval:
        approval = await anyio.to_thread.run_sync(self._get_approval, approval_id)
        if approval is None:
            raise KeyError(approval_id)
        return approval

    def _get_approval(self, approval_id: str) -> Approval | None:
        with self._connect() as conn:
            row = conn.execute(
                "select data_json from approvals where id = ?", (approval_id,)
            ).fetchone()
        return Approval.model_validate_json(row[0]) if row else None

    async def get_invocation(self, tool_call_id: str) -> ToolInvocation:
        invocation = await anyio.to_thread.run_sync(self._get_invocation, tool_call_id)
        if invocation is None:
            raise KeyError(tool_call_id)
        return invocation

    def _get_invocation(self, tool_call_id: str) -> ToolInvocation | None:
        with self._connect() as conn:
            row = conn.execute(
                "select data_json from tool_invocations where tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
        return ToolInvocation.model_validate_json(row[0]) if row else None

    async def put_artifact(self, run_id: str, kind: str, content: str) -> Artifact:
        return await anyio.to_thread.run_sync(self._put_artifact, run_id, kind, content)

    def _put_artifact(self, run_id: str, kind: str, content: str) -> Artifact:
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

    async def get_artifact_text(self, artifact_id: str) -> str:
        content = await anyio.to_thread.run_sync(self._get_artifact_text, artifact_id)
        if content is None:
            raise KeyError(artifact_id)
        return content

    def _get_artifact_text(self, artifact_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "select content from artifacts where id = ?", (artifact_id,)
            ).fetchone()
        return row[0] if row else None
