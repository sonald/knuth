from __future__ import annotations

from typing import Any

from pydantic import Field

from knuth.core.types import EventDurability, KnuthModel


class RuntimeEvent(KnuthModel):
    id: str
    run_id: str
    seq: int
    namespace: str
    name: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    durability: EventDurability = EventDurability.DURABLE
    created_at: str
