"""Maintenance writer for ``MessageProjectionCheckpoint`` cache facts.

The writer reads the current run, decides whether the policy threshold for a
fresh checkpoint is met, folds the model-visible projection through the
current ``run.last_seq`` from raw events (never chaining off an older
checkpoint), and appends one ``message.projection_checkpoint`` event.

It is intentionally not a :class:`MessageMiddleware`: it does not produce
patches, does not participate in middleware priority ordering, and does not
change which messages the model sees. Failures are non-fatal — a missed
checkpoint only degrades load-time performance, never run semantics.
"""

from __future__ import annotations

import logging

from knuth.core.runtime_events import (
    CheckpointTapeMessage,
    MessageProjectionCheckpointDraft,
)
from knuth.core.types import KnuthModel, RunStatus

from knuth_runtime.context import (
    MessageTape,
    load_message_tape_without_checkpoint,
)
from knuth_runtime.ledger import LedgerError, RunLedger

_LOG = logging.getLogger(__name__)


class ProjectionCheckpointPolicy(KnuthModel):
    """Simple threshold gate: only checkpoint when both knobs say it's worth
    the write.

    ``min_events_since_checkpoint`` keeps very short runs out of the cache
    write path; ``min_messages`` keeps the payload large enough that a fast
    path actually saves a fold over many small re-projections.
    """

    min_events_since_checkpoint: int = 200
    min_messages: int = 8


# Statuses where the runtime currently calls ``maybe_append``: the SUCCEEDED
# safe point reached at the end of a clean turn. Other statuses are either
# still mid-decision (RUNNING with an open batch, WAITING_*) or terminal
# without a turn-close hook. INTERRUPTED is deliberately excluded — the
# interrupt-collapse path does not invoke this writer today, so leaving it on
# the allowlist would advertise wiring that does not exist.
_SAFE_BOUNDARY_STATUSES = frozenset({RunStatus.SUCCEEDED})


class ProjectionCheckpointWriter:
    """Writes ``message.projection_checkpoint`` events at safe boundaries."""

    def __init__(
        self,
        ledger: RunLedger,
        policy: ProjectionCheckpointPolicy | None = None,
    ) -> None:
        self.ledger = ledger
        self.policy = policy or ProjectionCheckpointPolicy()

    async def maybe_append(self, run_id: str) -> bool:
        """Append a checkpoint when the policy permits and the boundary is safe.

        Returns ``True`` if a checkpoint was written, ``False`` otherwise. Any
        transient error is logged and swallowed; the run continues.
        """
        try:
            run = await self.ledger.get_run(run_id)
        except KeyError:
            return False

        if not self._safe_boundary(run.status, run.open_batch_id):
            return False

        latest = await self.ledger.latest_message_projection_checkpoint(run_id)
        baseline_seq = latest.through_seq if latest is not None else 0
        if run.last_seq - baseline_seq < self.policy.min_events_since_checkpoint:
            return False

        try:
            tape = await load_message_tape_without_checkpoint(
                self.ledger, run_id, through_seq=run.last_seq
            )
        except Exception:  # pragma: no cover - structural ledger errors surface elsewhere
            _LOG.warning(
                "projection checkpoint skipped: tape fold failed",
                extra={"run_id": run_id, "through_seq": run.last_seq},
                exc_info=True,
            )
            return False

        visible = tape.model_visible()
        if len(visible) < self.policy.min_messages:
            return False

        payload = [
            CheckpointTapeMessage(
                id=item.id,
                message=item.message,
                origin=item.origin,
                metadata=dict(item.metadata),
            )
            for item in visible
        ]
        try:
            await self.ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=run.last_seq,
                    messages=payload,
                ),
            )
        except LedgerError as exc:
            # A concurrent durable write between our read and append moved
            # ``run.last_seq``, so the through_seq invariant rejects the draft.
            # That's the documented "skip and try next safe point" path.
            _LOG.info(
                "projection checkpoint skipped: %s",
                exc,
                extra={"run_id": run_id, "through_seq": run.last_seq},
            )
            return False
        return True

    @staticmethod
    def _safe_boundary(status: RunStatus, open_batch_id: str | None) -> bool:
        return open_batch_id is None and status in _SAFE_BOUNDARY_STATUSES
