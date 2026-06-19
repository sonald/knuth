"""Translate Knuth ``RuntimeEvent`` values into AG-UI events.

The two event models line up almost one-to-one (see docs/decisions for the
mapping rationale), but AG-UI needs explicit lifecycle bracketing that Knuth's
stream leaves implicit: a streamed assistant message must be wrapped in
START/END, and a thinking region in THINKING_START/END. This translator is the
small state machine that adds those brackets.

Design choices:

* Tool calls are driven from the durable ``tool.batch_planned`` event, which
  always carries the full name + arguments, rather than from the transient
  ``model.tool_call.*`` deltas. This is always correct regardless of whether
  the provider streams tool arguments. Token-level argument streaming (using
  the transient deltas) is a later enhancement, not a correctness gap.
* Exactly one terminal event is emitted, at ``run.invocation.ended``. Mid-run
  ``model.failed`` only closes open regions; the loop may still recover.
* Knuth's ``approval.requested`` is surfaced as an AG-UI CUSTOM event so a
  frontend can render a human-in-the-loop prompt. Approval resolution and
  resume stay separate control operations.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

from knuth.core.events import (
    ApprovalRequested,
    ModelContentDelta,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    RuntimeEvent,
    RunInvocationEnded,
    RunInvocationStarted,
    ToolBatchPlanned,
    ToolInvocationAwaitingExternalResult,
    ToolInvocationCompleted,
)
from knuth.core.types import RunStatus

from knuth_agui import events as ag

_AGUIHandler = Callable[..., list[ag.AGUIEvent]]


class AGUITranslator:
    def __init__(self, thread_id: str, run_id: str) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self._text_message_id: str | None = None
        self._thinking_open = False
        self._dispatch: dict[str, _AGUIHandler] = {
            "run.invocation.started": self._on_run_started,
            "run.invocation.ended": self._on_run_ended,
            "model.content.delta": self._on_content_delta,
            "model.reasoning.delta": self._on_reasoning_delta,
            "model.reasoning.completed": self._on_reasoning_completed,
            "tool.batch_planned": self._on_batch_planned,
            "tool.invocation_awaiting_external_result": (
                self._on_tool_awaiting_external_result
            ),
            "tool.invocation_completed": self._on_tool_completed,
            "approval.requested": self._on_approval_requested,
        }

    def translate(self, event: RuntimeEvent) -> list[ag.AGUIEvent]:
        handler = self._dispatch.get(event.type)
        return handler(event) if handler is not None else []

    # -- region bracketing -------------------------------------------------

    def _open_text(self) -> list[ag.AGUIEvent]:
        if self._text_message_id is not None:
            return []
        self._text_message_id = f"msg_{uuid4().hex}"
        return [ag.text_message_start(self._text_message_id)]

    def _close_text(self) -> list[ag.AGUIEvent]:
        if self._text_message_id is None:
            return []
        out = [ag.text_message_end(self._text_message_id)]
        self._text_message_id = None
        return out

    def _close_thinking(self) -> list[ag.AGUIEvent]:
        if not self._thinking_open:
            return []
        self._thinking_open = False
        return [ag.thinking_text_end(), ag.thinking_end()]

    def _close_open(self) -> list[ag.AGUIEvent]:
        return self._close_thinking() + self._close_text()

    # -- handlers ----------------------------------------------------------

    def _on_run_started(self, _event: RunInvocationStarted) -> list[ag.AGUIEvent]:
        return [ag.run_started(self.thread_id, self.run_id)]

    def _on_content_delta(self, event: ModelContentDelta) -> list[ag.AGUIEvent]:
        out = self._close_thinking()
        out += self._open_text()
        assert self._text_message_id is not None
        out.append(ag.text_message_content(self._text_message_id, event.delta))
        return out

    def _on_reasoning_delta(self, event: ModelReasoningDelta) -> list[ag.AGUIEvent]:
        out = self._close_text()
        if not self._thinking_open:
            self._thinking_open = True
            out += [ag.thinking_start(), ag.thinking_text_start()]
        out.append(ag.thinking_text_content(event.delta))
        return out

    def _on_reasoning_completed(
        self, _event: ModelReasoningCompleted
    ) -> list[ag.AGUIEvent]:
        return self._close_thinking()

    def _on_batch_planned(self, event: ToolBatchPlanned) -> list[ag.AGUIEvent]:
        out = self._close_open()
        for call in event.calls:
            out.append(ag.tool_call_start(call.tool_call_id, call.name))
            out.append(
                ag.tool_call_args(call.tool_call_id, json.dumps(call.args, ensure_ascii=False))
            )
            out.append(ag.tool_call_end(call.tool_call_id))
        return out

    def _on_tool_completed(self, event: ToolInvocationCompleted) -> list[ag.AGUIEvent]:
        content = event.observation
        if event.outcome == "denied":
            content = content or "Tool call denied."
        return [
            ag.tool_call_result(
                message_id=f"msg_{uuid4().hex}",
                tool_call_id=event.tool_call_id,
                content=str(content),
            )
        ]

    def _on_tool_awaiting_external_result(
        self, event: ToolInvocationAwaitingExternalResult
    ) -> list[ag.AGUIEvent]:
        return [
            ag.custom(
                "knuth.tool_result_required",
                {
                    "runId": self.run_id,
                    "threadId": self.thread_id,
                    "toolCallId": event.tool_call_id,
                    "toolName": event.tool_name,
                    "args": event.args,
                },
            )
        ]

    def _on_approval_requested(self, event: ApprovalRequested) -> list[ag.AGUIEvent]:
        return [
            ag.custom(
                "knuth.approval_requested",
                {
                    "approvalId": event.approval_id,
                    "toolCallId": event.tool_call_id,
                    "title": event.title,
                    "reason": event.reason,
                    "risk": event.risk,
                    "preview": event.approval_preview,
                },
            )
        ]

    def _on_run_ended(self, event: RunInvocationEnded) -> list[ag.AGUIEvent]:
        out = self._close_open()
        status = event.status
        if event.error is not None or status == RunStatus.FAILED:
            message = event.error.message if event.error is not None else "run failed"
            code = event.error.code if event.error is not None else None
            out.append(ag.run_error(message, code))
        else:
            out.append(ag.run_finished(self.thread_id, self.run_id))
        return out
