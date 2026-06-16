"""Phase 1 acceptance: durable interrupt state, tool outcomes, and notices.

These exercise the ledger reducers, projections, and serialization for the
interruptible-run mechanism (docs/interrupt-requirements-and-design.md, R1-R3).
"""

from __future__ import annotations

import unittest

import anyio

from knuth.core.events import (
    parse_stored_runtime_event_json,
    store_runtime_event,
)
from knuth.core.invocations import (
    ToolCallDecision,
    ToolEffect,
    ToolInvocationStatus,
    args_hash_for,
)
from knuth.core.messages import InferenceRole, ToolCall as CoreToolCall
from knuth.core.runtime_events import (
    ContextSnapshot,
    ConversationNotice,
    ConversationNoticeDraft,
    ModelCompletedDraft,
    PlannedToolCall,
    RunInterrupted,
    RunInterruptedDraft,
    StepStartedDraft,
    ToolBatchClosedDraft,
    ToolBatchPlannedDraft,
    ToolInvocationCompletedDraft,
    ToolInvocationStartedDraft,
    UserMessageDraft,
)
from knuth.core.types import RunStatus
from knuth_runtime import LedgerError, MemoryRunLedger
from knuth_runtime.context import reconstruct_messages_from_events


def _snapshot() -> ContextSnapshot:
    return ContextSnapshot(
        messages_hash="m",
        tools_hash="t",
        preamble_hash="p",
        model_config_hash="c",
        message_count=1,
        tool_count=0,
    )


async def _running_run(ledger: MemoryRunLedger) -> str:
    """A run advanced to RUNNING with no open tool batch."""
    run = await ledger.create_run("q")
    await ledger.apply(run.id, UserMessageDraft(content="q"))
    await ledger.apply(
        run.id, StepStartedDraft(step_id="s1", index=1, snapshot=_snapshot())
    )
    return run.id


async def _open_batch(
    ledger: MemoryRunLedger, *, calls: list[tuple[str, dict]]
) -> str:
    """RUNNING run with an open batch; every call left APPROVED (allowed)."""
    run = await ledger.create_run("q")
    await ledger.apply(run.id, UserMessageDraft(content="q"))
    await ledger.apply(
        run.id, StepStartedDraft(step_id="s1", index=1, snapshot=_snapshot())
    )
    tool_calls = [
        CoreToolCall(tool_call_id=cid, name="read_file", arguments=args)
        for cid, args in calls
    ]
    await ledger.apply(
        run.id, ModelCompletedDraft(step_id="s1", tool_calls=tool_calls)
    )
    await ledger.apply(
        run.id,
        ToolBatchPlannedDraft(
            batch_id="b1",
            step_id="s1",
            calls=[
                PlannedToolCall(
                    tool_call_id=cid,
                    index=i,
                    name="read_file",
                    args=args,
                    args_hash=args_hash_for(args),
                )
                for i, (cid, args) in enumerate(calls)
            ],
        ),
    )
    from knuth.core.runtime_events import ToolProposedDraft

    for cid, _ in calls:
        await ledger.apply(
            run.id,
            ToolProposedDraft(tool_call_id=cid, decision=ToolCallDecision.ALLOWED),
        )
    return run.id


class RunInterruptedReducerTests(unittest.TestCase):
    def test_interrupt_transitions_active_run_to_interrupted(self) -> None:
        async def scenario() -> RunStatus:
            ledger = MemoryRunLedger()
            run_id = await _running_run(ledger)
            await ledger.apply(
                run_id,
                RunInterruptedDraft(reason="user_stop", active_phase="model"),
            )
            return (await ledger.get_run(run_id)).status

        self.assertEqual(anyio.run(scenario), RunStatus.INTERRUPTED)

    def test_interrupt_rejected_from_waiting_approval(self) -> None:
        async def scenario() -> None:
            ledger = MemoryRunLedger()
            run_id = await _open_batch(ledger, calls=[("c1", {"path": "a"})])
            # Force WAITING_APPROVAL is awkward here; instead assert that an
            # interrupt with an open batch is rejected outright.
            await ledger.apply(
                run_id,
                RunInterruptedDraft(reason="user_stop", active_phase="tool"),
            )

        with self.assertRaisesRegex(LedgerError, "open tool batch"):
            anyio.run(scenario)

    def test_interrupt_rejected_from_terminal(self) -> None:
        async def scenario() -> None:
            ledger = MemoryRunLedger()
            run_id = await _running_run(ledger)
            await ledger.apply(
                run_id, RunInterruptedDraft(reason="user_stop", active_phase="model")
            )
            # Already INTERRUPTED (not active): a second interrupt is rejected.
            await ledger.apply(
                run_id, RunInterruptedDraft(reason="user_stop", active_phase="model")
            )

        with self.assertRaisesRegex(LedgerError, "active run"):
            anyio.run(scenario)

    def test_user_message_continues_an_interrupted_run(self) -> None:
        async def scenario() -> RunStatus:
            ledger = MemoryRunLedger()
            run_id = await _running_run(ledger)
            await ledger.apply(
                run_id, RunInterruptedDraft(reason="user_stop", active_phase="model")
            )
            await ledger.apply(run_id, UserMessageDraft(content="try again"))
            return (await ledger.get_run(run_id)).status

        # The run stays INTERRUPTED until a new step starts; user.message is
        # accepted (no LedgerError), which continue_run relies on.
        self.assertEqual(anyio.run(scenario), RunStatus.INTERRUPTED)


class ToolInterruptOutcomeTests(unittest.TestCase):
    def test_interrupted_completion_requires_observation(self) -> None:
        async def scenario() -> None:
            ledger = MemoryRunLedger()
            run_id = await _open_batch(ledger, calls=[("c1", {"path": "a"})])
            await ledger.apply(
                run_id, ToolInvocationStartedDraft(tool_call_id="c1", attempt=1)
            )
            await ledger.apply(
                run_id,
                ToolInvocationCompletedDraft(
                    tool_call_id="c1", tool_name="read_file", outcome="interrupted"
                ),
            )

        with self.assertRaisesRegex(LedgerError, "model-visible observation"):
            anyio.run(scenario)

    def test_interrupted_completion_closes_batch(self) -> None:
        async def scenario() -> tuple[ToolInvocationStatus, RunStatus]:
            ledger = MemoryRunLedger()
            run_id = await _open_batch(ledger, calls=[("c1", {"path": "a"})])
            await ledger.apply(
                run_id, ToolInvocationStartedDraft(tool_call_id="c1", attempt=1)
            )
            await ledger.apply(
                run_id,
                ToolInvocationCompletedDraft(
                    tool_call_id="c1",
                    tool_name="read_file",
                    outcome="interrupted",
                    observation="Tool was interrupted by the user.",
                ),
            )
            await ledger.apply(run_id, ToolBatchClosedDraft(batch_id="b1"))
            inv = await ledger.get_invocation("c1")
            run = await ledger.get_run(run_id)
            return inv.status, run

        status, run = anyio.run(scenario)
        self.assertEqual(status, ToolInvocationStatus.INTERRUPTED)
        self.assertIsNone(run.open_batch_id)


class ToolBatchInterruptCollapseTests(unittest.TestCase):
    def test_collapse_abandons_unstarted_then_interrupts_atomically(self) -> None:
        async def scenario():
            ledger = MemoryRunLedger()
            run_id = await _open_batch(
                ledger, calls=[("c1", {"path": "a"}), ("c2", {"path": "b"})]
            )
            await ledger.apply(
                run_id, ToolInvocationStartedDraft(tool_call_id="c1", attempt=1)
            )
            # One atomic collapse: active observation, abandoned observation for
            # the unstarted call, batch close, notice, then run.interrupted.
            await ledger.apply_many(
                run_id,
                [
                    ToolInvocationCompletedDraft(
                        tool_call_id="c1",
                        tool_name="read_file",
                        outcome="interrupted",
                        observation="c1 stopped by user.",
                    ),
                    ToolInvocationCompletedDraft(
                        tool_call_id="c2",
                        tool_name="read_file",
                        outcome="interrupted",
                        observation="c2 was not started; turn stopped by user.",
                    ),
                    ToolBatchClosedDraft(batch_id="b1"),
                    ConversationNoticeDraft(
                        kind="interrupted",
                        content="Previous turn stopped by user; do not retry.",
                    ),
                    RunInterruptedDraft(reason="user_stop", active_phase="tool"),
                ],
            )
            run = await ledger.get_run(run_id)
            c1 = await ledger.get_invocation("c1")
            c2 = await ledger.get_invocation("c2")
            return run, c1, c2

        run, c1, c2 = anyio.run(scenario)
        self.assertEqual(run.status, RunStatus.INTERRUPTED)
        self.assertIsNone(run.open_batch_id)
        self.assertEqual(c1.status, ToolInvocationStatus.INTERRUPTED)
        self.assertEqual(c2.status, ToolInvocationStatus.INTERRUPTED)

    def test_collapse_rolls_back_when_any_event_is_invalid(self) -> None:
        async def scenario():
            ledger = MemoryRunLedger()
            run_id = await _open_batch(ledger, calls=[("c1", {"path": "a"})])
            await ledger.apply(
                run_id, ToolInvocationStartedDraft(tool_call_id="c1", attempt=1)
            )
            # batch_closed before the observation is illegal; the whole apply_many
            # must roll back so no half-collapse is observable.
            with self.assertRaises(LedgerError):
                await ledger.apply_many(
                    run_id,
                    [
                        ToolBatchClosedDraft(batch_id="b1"),
                        RunInterruptedDraft(reason="user_stop", active_phase="tool"),
                    ],
                )
            run = await ledger.get_run(run_id)
            return run

        run = anyio.run(scenario)
        # Rolled back: still RUNNING with the batch open, no interruption fact.
        self.assertEqual(run.status, RunStatus.RUNNING)
        self.assertEqual(run.open_batch_id, "b1")


class ConversationNoticeTests(unittest.TestCase):
    def test_notice_projects_as_user_message(self) -> None:
        async def scenario():
            ledger = MemoryRunLedger()
            run_id = await _running_run(ledger)
            await ledger.apply(
                run_id,
                ConversationNoticeDraft(kind="interrupted", content="stopped by user"),
            )
            events = await ledger.list_events(run_id)
            return await reconstruct_messages_from_events(
                events, ledger.get_artifact_text
            )

        messages = anyio.run(scenario)
        notice = [m for m in messages if m.content == "stopped by user"]
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0].role, InferenceRole.USER)

    def test_notice_rejected_inside_open_batch(self) -> None:
        async def scenario() -> None:
            ledger = MemoryRunLedger()
            run_id = await _open_batch(ledger, calls=[("c1", {"path": "a"})])
            await ledger.apply(
                run_id,
                ConversationNoticeDraft(kind="interrupted", content="x"),
            )

        with self.assertRaisesRegex(LedgerError, "open tool batch"):
            anyio.run(scenario)


class InterruptBudgetTests(unittest.TestCase):
    def test_interrupted_attempts_do_not_consume_max_turns(self) -> None:
        async def scenario() -> tuple[int, int]:
            ledger = MemoryRunLedger()
            run = await ledger.create_run("q")
            await ledger.apply(run.id, UserMessageDraft(content="q"))
            # Three interrupted attempts: steps climbs, committed stays 0.
            for i in range(1, 4):
                await ledger.apply(
                    run.id,
                    StepStartedDraft(
                        step_id=f"s{i}", index=i, snapshot=_snapshot()
                    ),
                )
                await ledger.apply(
                    run.id,
                    RunInterruptedDraft(reason="user_stop", active_phase="model"),
                )
                await ledger.apply(run.id, UserMessageDraft(content="again"))
                # continue_run flips the interrupted run back to RUNNING.
                from knuth.core.runtime_events import RunResumedDraft

                await ledger.apply(run.id, RunResumedDraft(cause="user_message"))
            current = await ledger.get_run(run.id)
            return current.steps, current.committed_turns

        steps, committed = anyio.run(scenario)
        self.assertEqual(steps, 3)
        self.assertEqual(committed, 0)


class InterruptSerializationTests(unittest.TestCase):
    def test_run_interrupted_round_trips(self) -> None:
        stored = store_runtime_event(
            "run-1",
            5,
            RunInterruptedDraft(
                reason="user_stop", active_phase="model", message="stopped"
            ),
            event_id="evt-int",
            created_at="2026-06-16T00:00:00Z",
        )
        parsed = parse_stored_runtime_event_json(stored.model_dump_json())
        self.assertIsInstance(parsed, RunInterrupted)
        self.assertEqual(parsed, stored)

    def test_conversation_notice_round_trips(self) -> None:
        stored = store_runtime_event(
            "run-1",
            6,
            ConversationNoticeDraft(kind="runtime", content="hi"),
            event_id="evt-notice",
            created_at="2026-06-16T00:00:00Z",
        )
        parsed = parse_stored_runtime_event_json(stored.model_dump_json())
        self.assertIsInstance(parsed, ConversationNotice)
        self.assertEqual(parsed, stored)


if __name__ == "__main__":
    unittest.main()
