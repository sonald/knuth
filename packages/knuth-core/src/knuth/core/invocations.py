from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import Field

from knuth.core.types import KnuthModel


class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolEffect(StrEnum):
    PURE = "pure"
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    DANGEROUS = "dangerous"


RETRYABLE_EFFECTS = frozenset(
    {ToolEffect.PURE, ToolEffect.READ, ToolEffect.LOCAL_WRITE}
)
EXTERNAL_EFFECTS = frozenset({ToolEffect.EXTERNAL_WRITE, ToolEffect.DANGEROUS})


class ToolCallDecision(StrEnum):
    ALLOWED = "allowed"
    REQUIRES_APPROVAL = "requires_approval"
    DENIED = "denied"


class ToolInvocationStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ToolInvocation(KnuthModel):
    """Per-tool-call state machine projection; the unit the loop schedules."""

    tool_call_id: str
    run_id: str
    batch_id: str
    step_id: str
    index: int = 0
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    args_hash: str
    status: ToolInvocationStatus = ToolInvocationStatus.PROPOSED
    effect: ToolEffect = ToolEffect.READ
    risk: ToolRisk = ToolRisk.LOW
    approval_id: str | None = None
    idempotency_key: str | None = None
    attempts: int = 0
    observed: bool = False
    denial_reason: str | None = None
    updated_seq: int = 0


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class Approval(KnuthModel):
    """Approval bound to one tool call and its exact arguments."""

    id: str
    run_id: str
    tool_call_id: str
    args_hash: str
    status: ApprovalStatus
    title: str
    reason: str
    risk: str
    preview: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    resolved_at: str | None = None
    resolved_by: str | None = None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def args_hash_for(args: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()


def approval_id_for(run_id: str, tool_call_id: str) -> str:
    return f"appr_{run_id}_{tool_call_id}"
