"""Subprocess driver used by real PTY REPL smoke tests."""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass
from types import SimpleNamespace

from knuth.core.events import ModelContentDeltaDraft, emit_transient_runtime_event
from knuth.core.types import RunStatus
from knuth_cli.cli import main
from knuth_runtime import RunResult


@dataclass
class FakeApproval:
    id: str
    tool_call_id: str
    title: str
    approval_preview: dict[str, str]


class FakeRuntime:
    def __init__(self) -> None:
        self._approvals: list[FakeApproval] = []

    def start(self, prompt: str, *, listeners=()):
        if prompt == "approval":
            self._approvals = [
                FakeApproval(
                    id="approval-1",
                    tool_call_id="tool-call-1",
                    title="Read file",
                    approval_preview={"tool": "read_file"},
                )
            ]
            return FakeRunSession(
                answer="waiting",
                run_id="approval-run",
                status=RunStatus.WAITING_APPROVAL,
                listeners=listeners,
            )
        return FakeRunSession(
            answer=f"fake answer: {prompt}",
            run_id="run-1",
            status=RunStatus.SUCCEEDED,
            listeners=listeners,
        )

    def continue_run(self, run_id: str, prompt: str, *, listeners=()):
        return FakeRunSession(
            answer=f"fake answer: {prompt}",
            run_id=run_id,
            status=RunStatus.SUCCEEDED,
            listeners=listeners,
        )

    def resume(self, run_id: str, *, listeners=()):
        return FakeRunSession(
            answer="fake answer: resumed",
            run_id=run_id,
            status=RunStatus.SUCCEEDED,
            listeners=listeners,
        )

    async def pending_approvals(self, run_id: str):
        return list(self._approvals) if run_id == "approval-run" else []

    async def approve(self, approval_id: str):
        self._approvals = [
            approval for approval in self._approvals if approval.id != approval_id
        ]
        return SimpleNamespace(id=approval_id)

    async def deny(self, approval_id: str):
        self._approvals = [
            approval for approval in self._approvals if approval.id != approval_id
        ]
        return SimpleNamespace(id=approval_id)

    async def runs(self, limit: int = 20):
        if os.environ.get("KNUTH_TEST_RUNS") != "1":
            return []
        return [SimpleNamespace(id="run-1", status=RunStatus.PAUSED)]

    async def tools(self):
        return [
            {
                "type": "function",
                "function": {"name": "read_file", "description": "Read file"},
            }
        ]


class FakeRunSession:
    def __init__(
        self, *, answer: str, run_id: str, status: RunStatus, listeners=()
    ) -> None:
        self.run_id = run_id
        self._answer = answer
        self._status = status
        self._listeners = tuple(listeners)

    async def __aenter__(self):
        if self._answer:
            event = emit_transient_runtime_event(
                self.run_id,
                ModelContentDeltaDraft(delta=self._answer),
                event_id="evt-1",
                created_at="2026-06-21T00:00:00Z",
            )
            for listener in self._listeners:
                if listener.interest.matches(event):
                    await listener.handle_event(event)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def result(self) -> RunResult:
        return RunResult(answer=self._answer, run_id=self.run_id, status=self._status)


async def runtime_factory() -> FakeRuntime:
    return FakeRuntime()


if __name__ == "__main__":
    sys.exit(main(["run"], runtime_factory=runtime_factory))
