from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from knuth.core.events import RuntimeEvent
from knuth_runtime.observation import (
    RuntimeEventInterest,
    RuntimeEventOverflowPolicy,
)

from knuth_cli.runtime_event_tui.models import ObservedEventRow

OnEventRow = Callable[[ObservedEventRow], Awaitable[None] | None]


class RuntimeEventCapture:
    interest = RuntimeEventInterest.all()
    overflow_policy = RuntimeEventOverflowPolicy.BLOCK
    buffer_size = 1000

    def __init__(self, on_row: OnEventRow | None = None) -> None:
        self._rows: list[ObservedEventRow] = []
        self._on_row = on_row

    @property
    def rows(self) -> tuple[ObservedEventRow, ...]:
        return tuple(self._rows)

    async def handle_event(self, event: RuntimeEvent) -> None:
        row = ObservedEventRow.from_event(
            event,
            source="live",
            receive_index=len(self._rows) + 1,
        )
        self._rows.append(row)
        if self._on_row is not None:
            maybe_awaitable = self._on_row(row)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
