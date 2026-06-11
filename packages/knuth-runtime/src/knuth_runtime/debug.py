from __future__ import annotations

from pathlib import Path

import anyio

from knuth.core.events import RuntimeEvent

from knuth_runtime.observation import (
    RuntimeEventInterest,
    RuntimeEventOverflowPolicy,
)

DEFAULT_DEBUG_SINK_DIR = Path("~/.knuth/debug")


class DebugEventSink:
    """Debug sink (design §8.4): one JSONL file per run, outside the ledger.

    Captures the full event stream including transient raw material —
    reasoning deltas and raw tool-call deltas exist nowhere else. The
    directory is not part of any durable contract: deleting it wholesale is
    the supported cleanup.

    Never load-bearing: drops events instead of stalling the run when the
    disk is slow, and a write failure disables the listener without touching
    the main flow.
    """

    overflow_policy = RuntimeEventOverflowPolicy.DROP_NEWEST
    buffer_size = 1000

    def __init__(self, directory: Path | str = DEFAULT_DEBUG_SINK_DIR) -> None:
        self._directory = Path(directory).expanduser()

    @property
    def interest(self) -> RuntimeEventInterest:
        return RuntimeEventInterest.all()

    async def handle_event(self, event: RuntimeEvent) -> None:
        await anyio.to_thread.run_sync(
            self._append, event.run_id, event.model_dump_json()
        )

    def _append(self, run_id: str, line: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        with (self._directory / f"{run_id}.jsonl").open(
            "a", encoding="utf-8"
        ) as sink:
            sink.write(line + "\n")


__all__ = ["DEFAULT_DEBUG_SINK_DIR", "DebugEventSink"]
