from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from knuth.core.events import (
    DurableRuntimeEventDraft,
    RuntimeEvent,
    RuntimeEventDraft,
    TransientRuntimeEventDraft,
    emit_transient_runtime_event,
)
from knuth.core.types import EventDurability

from knuth_runtime.interrupts import InterruptController
from knuth_runtime.observation import LiveRuntimeObservation
from knuth_runtime.services import RuntimeServices

RunInvocationMode = Literal["start", "continue", "resume"]


@dataclass
class RuntimeInvocation:
    run_id: str
    mode: RunInvocationMode
    services: RuntimeServices
    observation: LiveRuntimeObservation
    interrupts: InterruptController

    @property
    def interrupt_signal(self):
        """The invocation-scoped signal handed to the loop, llmd, and tools."""
        return self.interrupts.signal

    async def emit(self, event: RuntimeEventDraft) -> RuntimeEvent:
        if event.durability == EventDurability.DURABLE:
            runtime_event = await self.services.ledger.apply(
                self.run_id,
                cast(DurableRuntimeEventDraft, event),
            )
        else:
            runtime_event = emit_transient_runtime_event(
                self.run_id,
                cast(TransientRuntimeEventDraft, event),
                event_id=f"evt_{uuid4().hex}",
                created_at=datetime.now(UTC).isoformat(),
            )
        await self.observation.publish(runtime_event)
        return runtime_event

    async def emit_many(
        self, drafts: list[DurableRuntimeEventDraft]
    ) -> list[RuntimeEvent]:
        """Commit several durable drafts atomically, then publish them live.

        Semantic collapses such as the tool-batch interrupt safe point must be
        one ledger transaction; the live publish happens only after the durable
        write succeeds, so observers never see a half-applied collapse.
        """
        events = await self.services.ledger.apply_many(self.run_id, drafts)
        for event in events:
            await self.observation.publish(event)
        return events
