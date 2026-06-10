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
from knuth_runtime.observation import LiveRuntimeObservation
from knuth_runtime.services import RuntimeServices

RunInvocationMode = Literal["start", "continue", "resume"]


@dataclass
class RuntimeInvocation:
    run_id: str
    mode: RunInvocationMode
    services: RuntimeServices
    observation: LiveRuntimeObservation

    async def emit(self, event: RuntimeEventDraft) -> RuntimeEvent:
        if event.durability == EventDurability.DURABLE:
            runtime_event = await self.services.event_store.append(
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
