from __future__ import annotations

from enum import StrEnum
from typing import Any

import anyio
from pydantic import Field

from knuth.core.types import KnuthModel
from knuth_runtime.stores import SQLiteStore, utc_now
from knuth_toold.broker import ApprovalRequest


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class Approval(KnuthModel):
    id: str
    run_id: str
    status: ApprovalStatus
    title: str
    reason: str
    risk: str
    payload: dict[str, Any]
    created_at: str
    resolved_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryApprovalService:
    def __init__(self) -> None:
        self._approvals: dict[str, Approval] = {}

    async def request(self, request: ApprovalRequest) -> Approval:
        existing = self._approvals.get(request.id)
        if existing is not None:
            return existing
        approval = Approval(
            id=request.id,
            run_id=request.run_id,
            status=ApprovalStatus.PENDING,
            title=request.title,
            reason=request.reason,
            risk=request.risk,
            payload=request.payload,
            created_at=utc_now(),
        )
        self._approvals[approval.id] = approval
        return approval

    async def resolve(self, approval_id: str, status: ApprovalStatus) -> Approval:
        approval = self._approvals[approval_id].model_copy(
            update={"status": status, "resolved_at": utc_now()}
        )
        self._approvals[approval_id] = approval
        return approval

    async def list_pending(self, run_id: str | None = None) -> list[Approval]:
        return [
            approval
            for approval in self._approvals.values()
            if approval.status == ApprovalStatus.PENDING
            and (run_id is None or approval.run_id == run_id)
        ]

    async def is_approved(self, approval_id: str) -> bool:
        approval = self._approvals.get(approval_id)
        return approval is not None and approval.status == ApprovalStatus.APPROVED


class SQLiteApprovalService(MemoryApprovalService):
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def _connect(self):
        import sqlite3

        return sqlite3.connect(self.store.db_path)

    async def request(self, request: ApprovalRequest) -> Approval:
        return await anyio.to_thread.run_sync(self._request, request)

    def _request(self, request: ApprovalRequest) -> Approval:
        with self._connect() as conn:
            row = conn.execute(
                "select data_json from approvals where id = ?", (request.id,)
            ).fetchone()
            if row is not None:
                return Approval.model_validate_json(row[0])
            approval = Approval(
                id=request.id,
                run_id=request.run_id,
                status=ApprovalStatus.PENDING,
                title=request.title,
                reason=request.reason,
                risk=request.risk,
                payload=request.payload,
                created_at=utc_now(),
            )
            conn.execute(
                "insert into approvals values (?, ?, ?, ?, ?, ?)",
                (
                    approval.id,
                    approval.run_id,
                    approval.status.value,
                    approval.model_dump_json(),
                    approval.created_at,
                    approval.resolved_at,
                ),
            )
            return approval

    async def resolve(self, approval_id: str, status: ApprovalStatus) -> Approval:
        return await anyio.to_thread.run_sync(self._resolve, approval_id, status)

    def _resolve(self, approval_id: str, status: ApprovalStatus) -> Approval:
        with self._connect() as conn:
            row = conn.execute(
                "select data_json from approvals where id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise KeyError(approval_id)
            approval = Approval.model_validate_json(row[0]).model_copy(
                update={"status": status, "resolved_at": utc_now()}
            )
            conn.execute(
                "update approvals set status = ?, data_json = ?, resolved_at = ? where id = ?",
                (
                    approval.status.value,
                    approval.model_dump_json(),
                    approval.resolved_at,
                    approval.id,
                ),
            )
            return approval

    async def list_pending(self, run_id: str | None = None) -> list[Approval]:
        return await anyio.to_thread.run_sync(self._list_pending, run_id)

    def _list_pending(self, run_id: str | None = None) -> list[Approval]:
        sql = "select data_json from approvals where status = ?"
        params: tuple[Any, ...] = (ApprovalStatus.PENDING.value,)
        if run_id is not None:
            sql += " and run_id = ?"
            params = (ApprovalStatus.PENDING.value, run_id)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Approval.model_validate_json(row[0]) for row in rows]

    async def is_approved(self, approval_id: str) -> bool:
        return await anyio.to_thread.run_sync(self._is_approved, approval_id)

    def _is_approved(self, approval_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select status from approvals where id = ?", (approval_id,)
            ).fetchone()
        return row is not None and row[0] == ApprovalStatus.APPROVED.value
