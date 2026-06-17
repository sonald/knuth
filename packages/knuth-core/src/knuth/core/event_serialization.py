from __future__ import annotations

from typing import Annotated, Any, get_args

from pydantic import Field, TypeAdapter

from knuth.core.runtime_events import (
    DurableRuntimeEventDraft,
    StoredRuntimeEvent,
    StoredRuntimeEventBase,
    TransientRuntimeEvent,
    TransientRuntimeEventBase,
    TransientRuntimeEventDraft,
)


def _registry_by_type(union: object) -> dict[str, type]:
    """Derive the type-tag -> event-class registry from a union.

    Every member declares ``type: Literal[...] = "..."``; the default is the
    tag. Keeping this derived (not hand-written) means adding an event class
    to the union is the only registration step.
    """
    return {cls.model_fields["type"].default: cls for cls in get_args(union)}


_STORED_EVENT_BY_TYPE: dict[str, type[StoredRuntimeEventBase]] = _registry_by_type(
    StoredRuntimeEvent
)

_TRANSIENT_EVENT_BY_TYPE: dict[str, type[TransientRuntimeEventBase]] = (
    _registry_by_type(TransientRuntimeEvent)
)

_STORED_RUNTIME_EVENT_ADAPTER: TypeAdapter[StoredRuntimeEvent] = TypeAdapter(
    Annotated[StoredRuntimeEvent, Field(discriminator="type")]
)


def store_runtime_event(
    run_id: str,
    seq: int,
    event: DurableRuntimeEventDraft,
    *,
    event_id: str,
    created_at: str,
    generated_fields: dict[str, Any] | None = None,
) -> StoredRuntimeEvent:
    event_class = _STORED_EVENT_BY_TYPE[event.type]
    payload = event.model_dump()
    if generated_fields:
        payload.update(generated_fields)
    return event_class(
        **payload,
        id=event_id,
        run_id=run_id,
        seq=seq,
        created_at=created_at,
    )


def emit_transient_runtime_event(
    run_id: str,
    event: TransientRuntimeEventDraft,
    *,
    event_id: str,
    created_at: str,
) -> TransientRuntimeEvent:
    event_class = _TRANSIENT_EVENT_BY_TYPE[event.type]
    return event_class(
        **event.model_dump(),
        id=event_id,
        run_id=run_id,
        created_at=created_at,
    )


def parse_stored_runtime_event_json(data: str) -> StoredRuntimeEvent:
    return _STORED_RUNTIME_EVENT_ADAPTER.validate_json(data)
