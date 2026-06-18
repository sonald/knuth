from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from knuth.core.events import RuntimeEvent
from knuth.core.types import RunStatus

from knuth_cli.runtime_event_tui.capture import RuntimeEventCapture
from knuth_cli.runtime_event_tui.models import ApprovalRow, ObservedEventRow, RunSnapshot
from knuth_cli.runtime_event_tui.views import dedupe_event_rows, latest_system_preamble

OnEventRow = Callable[[ObservedEventRow], Awaitable[None] | None]


def _status_text(status: object | None) -> str | None:
    if status is None:
        return None
    return getattr(status, "value", str(status))


class RuntimeEventTuiController:
    def __init__(
        self,
        runtime: Any,
        *,
        on_live_row: OnEventRow | None = None,
    ) -> None:
        self.runtime = runtime
        self.current_run_id: str | None = None
        self.live_rows: list[ObservedEventRow] = []
        self.last_snapshot: RunSnapshot | None = None
        self._on_live_row = on_live_row

    def set_live_row_callback(self, callback: OnEventRow | None) -> None:
        self._on_live_row = callback

    async def start(self, prompt: str, *, run_id: str | None = None) -> RunSnapshot:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is empty")
        self.current_run_id = run_id
        self.live_rows = []
        capture = RuntimeEventCapture(on_row=self._handle_live_row)
        kwargs: dict[str, Any] = {"listeners": [capture]}
        if run_id:
            kwargs["run_id"] = run_id
        async with self.runtime.start(prompt, **kwargs) as session:
            result = await session.result()
        self.current_run_id = result.run_id
        return await self.load_history(result.run_id, status=result.status)

    async def resume(self, run_id: str | None = None) -> RunSnapshot:
        target_run_id = run_id or self.current_run_id
        if not target_run_id:
            raise ValueError("run_id is required")
        self.current_run_id = target_run_id
        capture = RuntimeEventCapture(on_row=self._handle_live_row)
        async with self.runtime.resume(target_run_id, listeners=[capture]) as session:
            result = await session.result()
        self.current_run_id = result.run_id
        return await self.load_history(result.run_id, status=result.status)

    async def load_history(
        self,
        run_id: str,
        *,
        status: object | None = None,
        error: str | None = None,
    ) -> RunSnapshot:
        self.current_run_id = run_id
        durable_events = await self.runtime.events(run_id)
        durable_rows = [
            ObservedEventRow.from_event(event, source="durable")
            for event in durable_events
        ]
        rows = tuple(dedupe_event_rows([*self.live_rows, *durable_rows]))
        resolved_status = status
        if resolved_status is None and hasattr(self.runtime, "status"):
            resolved_status = await self.runtime.status(run_id)
        messages = tuple(await self.runtime.messages(run_id))
        model_context_messages = tuple(await self.runtime.model_context_messages(run_id))
        rewrite_audit = tuple(await self.runtime.rewrite_audit(run_id))
        approvals = tuple(
            ApprovalRow.from_approval(approval)
            for approval in await self.runtime.pending_approvals(run_id)
        )
        snapshot = RunSnapshot(
            run_id=run_id,
            status=_status_text(resolved_status),
            events=rows,
            messages=messages,
            model_context_messages=model_context_messages,
            rewrite_audit=rewrite_audit,
            approvals=approvals,
            latest_system_preamble=latest_system_preamble(rows),
            error=error,
        )
        self.last_snapshot = snapshot
        return snapshot

    async def approve(self, approval_id: str) -> RunSnapshot:
        approval = await self.runtime.approve(approval_id)
        run_id = getattr(approval, "run_id", None) or self.current_run_id
        if not run_id:
            raise ValueError("run_id is required after approval")
        return await self.load_history(run_id)

    async def deny(self, approval_id: str) -> RunSnapshot:
        approval = await self.runtime.deny(approval_id)
        run_id = getattr(approval, "run_id", None) or self.current_run_id
        if not run_id:
            raise ValueError("run_id is required after denial")
        return await self.load_history(run_id)

    async def _handle_live_row(self, row: ObservedEventRow) -> None:
        self.live_rows.append(row)
        if self._on_live_row is not None:
            result = self._on_live_row(row)
            if hasattr(result, "__await__"):
                await result
