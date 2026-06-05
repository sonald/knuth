from __future__ import annotations

from typing import Any

from pydantic import Field

from knuth.core.types import KnuthModel, RunStatus


class AgentRun(KnuthModel):
    id: str
    query: str
    status: RunStatus = RunStatus.CREATED
    created_at: str
    updated_at: str
    user_id: str | None = None
    parent_run_id: str | None = None
    title: str | None = None
    max_turns: int = 32
    budget: dict[str, Any] = Field(default_factory=dict)
