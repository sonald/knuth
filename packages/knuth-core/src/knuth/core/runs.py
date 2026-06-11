from __future__ import annotations

from knuth.core.types import KnuthModel, RunStatus


class AgentRun(KnuthModel):
    """Run projection row: status and cursor state folded from decision events."""

    id: str
    query: str
    status: RunStatus = RunStatus.CREATED
    created_at: str
    updated_at: str
    max_turns: int = 32

    # Projection cursor state (derived, rebuildable by refolding events).
    steps: int = 0
    open_batch_id: str | None = None
    current_step_id: str | None = None
    last_seq: int = 0


class Artifact(KnuthModel):
    """Immutable blob referenced by events; part of the ledger's side store."""

    id: str
    run_id: str
    kind: str
    sha256: str
    created_at: str
