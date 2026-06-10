from __future__ import annotations

from dataclasses import dataclass

from knuth.core.events import RuntimeEvent
from knuth.core.types import RunStatus


@dataclass(frozen=True)
class RunResult:
    answer: str
    run_id: str | None = None
    status: RunStatus | None = None


def answer_from_events(events: list[RuntimeEvent]) -> str:
    for event in reversed(events):
        if event.type == "run.succeeded":
            return event.answer
        if event.type == "approval.requested":
            return f"Waiting for approval: {event.approval_id}"
    return ""
