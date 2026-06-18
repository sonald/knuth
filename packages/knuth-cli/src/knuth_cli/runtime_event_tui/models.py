from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from knuth.core.events import RuntimeEvent
from knuth.core.messages import InferenceMessage

EventSource = Literal["live", "durable"]


@dataclass(frozen=True)
class ObservedEventRow:
    source: EventSource
    receive_index: int | None
    durable_seq: int | None
    event_type: str
    durability: str
    run_id: str
    event: RuntimeEvent

    @classmethod
    def from_event(
        cls,
        event: RuntimeEvent,
        *,
        source: EventSource,
        receive_index: int | None = None,
    ) -> "ObservedEventRow":
        durable_seq = getattr(event, "seq", None)
        return cls(
            source=source,
            receive_index=receive_index,
            durable_seq=durable_seq,
            event_type=event.type,
            durability=event.durability.value,
            run_id=event.run_id,
            event=event,
        )

    @property
    def durable_key(self) -> tuple[str, int] | tuple[str, str] | None:
        if self.durability != "durable":
            return None
        if self.durable_seq is not None:
            return (self.run_id, self.durable_seq)
        return (self.run_id, self.event.id)


@dataclass(frozen=True)
class ApprovalRow:
    approval_id: str
    run_id: str
    title: str
    status: str | None = None

    @classmethod
    def from_approval(cls, approval: Any) -> "ApprovalRow":
        status = getattr(approval, "status", None)
        if status is not None:
            status = getattr(status, "value", str(status))
        return cls(
            approval_id=approval.id,
            run_id=approval.run_id,
            title=getattr(approval, "title", ""),
            status=status,
        )


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str | None
    status: str | None
    events: tuple[ObservedEventRow, ...] = ()
    messages: tuple[InferenceMessage, ...] = ()
    model_context_messages: tuple[InferenceMessage, ...] = ()
    rewrite_audit: tuple[dict[str, Any], ...] = ()
    approvals: tuple[ApprovalRow, ...] = ()
    latest_system_preamble: str | None = None
    listener_stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
