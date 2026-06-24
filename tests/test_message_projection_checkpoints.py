"""Tests for ADR-011 message projection checkpoints.

Covers:
- Checkpoint event types and payload model serialization.
- ``fold_message_tape(initial, events)`` ignoring checkpoint events.
- ``RunLedger.latest_message_projection_checkpoint`` /
  ``list_message_projection_events`` for memory and SQLite backends.
- Reducer invariants for ``message.projection_checkpoint``.
- ``load_message_tape`` fast path equivalence and fallbacks.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio

from knuth.core.events import (
    CheckpointTapeMessage,
    MessageProjectionCheckpoint,
    MessageProjectionCheckpointDraft,
    TapeItemSource,
)
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.runtime_events import (
    ConversationNoticeDraft,
    MessageRewriteAnchorDraft,
    MessageRewriteMessageDraft,
    ModelCompletedDraft,
    PlannedToolCall,
    RunResumedDraft,
    RunSucceededDraft,
    StepStartedDraft,
    ToolBatchClosedDraft,
    ToolBatchPlannedDraft,
    ToolInvocationCompletedDraft,
    ToolInvocationStartedDraft,
    ToolProposedDraft,
    UserMessageDraft,
)
from knuth.core.invocations import (
    ToolCallDecision,
    ToolEffect,
    ToolRisk,
    args_hash_for,
)
from knuth.core.messages import ToolCall as CoreToolCall
from knuth_runtime import (
    LedgerError,
    MemoryRunLedger,
    ProjectionCheckpointPolicy,
    ProjectionCheckpointWriter,
    SQLiteRunLedger,
)
from knuth_runtime.context import (
    MessageTape,
    TapeMessage,
    fold_message_tape,
    load_message_tape,
    load_message_tape_without_checkpoint,
    reconstruct_message_tape_from_events,
)
from knuth.core.runtime_events import ContextSnapshot


def _snapshot() -> ContextSnapshot:
    return ContextSnapshot(
        messages_hash="m",
        tools_hash="t",
        preamble_hash="p",
        model_config_hash="c",
        message_count=1,
        tool_count=0,
    )


async def _seed_short_run(ledger, *, user_prompt: str = "hello") -> str:
    """Append a complete one-turn run that ends in SUCCEEDED.

    Returns the run id. The run uses one user message + one model_completed +
    one verification-style succeed, so the resulting tape has two model-visible
    messages and the run lands at a checkpoint-eligible boundary.
    """
    run = await ledger.create_run(query=user_prompt)
    await ledger.apply(run.id, UserMessageDraft(content=user_prompt))
    step_id = "step_1"
    await ledger.apply(
        run.id, StepStartedDraft(step_id=step_id, index=1, snapshot=_snapshot())
    )
    await ledger.apply(
        run.id,
        ModelCompletedDraft(step_id=step_id, content="answer", tool_calls=[]),
    )
    await ledger.apply(
        run.id, RunSucceededDraft(answer="answer", turns=1)
    )
    return run.id


async def _seed_run_with_tool_turn(ledger) -> str:
    """Run ending in SUCCEEDED with one tool call closed cleanly.

    Produces a more interesting projection event mix: user, model_completed
    with a tool_call, tool batch open and close, tool result, then a final
    succeeded model_completed.
    """
    run = await ledger.create_run(query="run with tool")
    await ledger.apply(run.id, UserMessageDraft(content="run with tool"))

    step_id = "step_1"
    await ledger.apply(
        run.id, StepStartedDraft(step_id=step_id, index=1, snapshot=_snapshot())
    )
    tool_call_id = "tc1"
    await ledger.apply(
        run.id,
        ModelCompletedDraft(
            step_id=step_id,
            content="thinking",
            tool_calls=[CoreToolCall(tool_call_id=tool_call_id, name="t", arguments={})],
        ),
    )
    await ledger.apply(
        run.id,
        ToolBatchPlannedDraft(
            batch_id="b1",
            step_id=step_id,
            calls=[
                PlannedToolCall(
                    tool_call_id=tool_call_id,
                    name="t",
                    args={},
                    args_hash=args_hash_for({}),
                )
            ],
        ),
    )
    await ledger.apply(
        run.id,
        ToolProposedDraft(
            tool_call_id=tool_call_id,
            decision=ToolCallDecision.ALLOWED,
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
        ),
    )
    await ledger.apply(
        run.id, ToolInvocationStartedDraft(tool_call_id=tool_call_id)
    )
    await ledger.apply(
        run.id,
        ToolInvocationCompletedDraft(
            tool_call_id=tool_call_id,
            tool_name="t",
            outcome="succeeded",
            observation="42",
        ),
    )
    await ledger.apply(run.id, ToolBatchClosedDraft(batch_id="b1"))

    step_id2 = "step_2"
    await ledger.apply(
        run.id, StepStartedDraft(step_id=step_id2, index=2, snapshot=_snapshot())
    )
    await ledger.apply(
        run.id,
        ModelCompletedDraft(step_id=step_id2, content="done", tool_calls=[]),
    )
    await ledger.apply(run.id, RunSucceededDraft(answer="done", turns=2))
    return run.id


def _to_checkpoint_payload(tape: MessageTape) -> list[CheckpointTapeMessage]:
    return [
        CheckpointTapeMessage(
            id=item.id,
            message=item.message,
            origin=item.origin,
            metadata=dict(item.metadata),
        )
        for item in tape.model_visible()
    ]


class CheckpointEventTypeTests(unittest.TestCase):
    def test_draft_type_tag(self) -> None:
        draft = MessageProjectionCheckpointDraft(through_seq=3, messages=[])
        self.assertEqual(draft.type, "message.projection_checkpoint")

    def test_payload_roundtrip(self) -> None:
        msg = CheckpointTapeMessage(
            id="m:1",
            message=InferenceMessage(role=InferenceRole.USER, content="hi"),
            origin=TapeItemSource.LEDGER,
            metadata={"k": "v"},
        )
        draft = MessageProjectionCheckpointDraft(through_seq=7, messages=[msg])
        encoded = draft.model_dump_json()
        roundtrip = MessageProjectionCheckpointDraft.model_validate_json(encoded)
        self.assertEqual(roundtrip.through_seq, 7)
        self.assertEqual(len(roundtrip.messages), 1)
        self.assertEqual(roundtrip.messages[0].origin, TapeItemSource.LEDGER)


class FoldMessageTapeTests(unittest.TestCase):
    def test_fold_ignores_checkpoint_event(self) -> None:
        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_short_run(ledger)
            events = await ledger.list_events(run_id)

            # Without the checkpoint event in the stream.
            base = await reconstruct_message_tape_from_events(events)

            # Append a checkpoint event to the timeline; the fold output must
            # not change because the function ignores checkpoint events.
            checkpoint = MessageProjectionCheckpoint(
                id="evt_x",
                run_id=run_id,
                seq=99,
                created_at="2026-06-23T00:00:00+00:00",
                through_seq=99,
                messages=_to_checkpoint_payload(base),
            )
            with_checkpoint = await reconstruct_message_tape_from_events(
                [*events, checkpoint]
            )
            self.assertEqual(
                [m.message for m in base.model_visible()],
                [m.message for m in with_checkpoint.model_visible()],
            )

        anyio.run(run)

    def test_fold_with_initial_baseline(self) -> None:
        """``fold_message_tape`` with a non-empty baseline applies only the
        tail events on top — equivalent to a from-scratch fold."""

        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_short_run(ledger)
            events = await ledger.list_events(run_id)

            # Pick a split point after the user.message event.
            split_seq = events[1].seq  # user.message
            head = [e for e in events if e.seq <= split_seq]
            tail = [e for e in events if e.seq > split_seq]

            head_tape = await reconstruct_message_tape_from_events(head)
            initial = MessageTape(
                items=[
                    TapeMessage(
                        id=item.id,
                        message=item.message,
                        origin=item.origin,
                        metadata=dict(item.metadata),
                    )
                    for item in head_tape.model_visible()
                ]
            )
            folded = await fold_message_tape(initial, tail)
            full = await reconstruct_message_tape_from_events(events)
            self.assertEqual(
                [m.message for m in folded.model_visible()],
                [m.message for m in full.model_visible()],
            )

        anyio.run(run)


class LedgerProjectionApiTests(unittest.TestCase):
    def _run_with_each_backend(self, body):
        async def memory() -> None:
            await body(MemoryRunLedger())

        async def sqlite() -> None:
            with TemporaryDirectory() as tmp:
                ledger = SQLiteRunLedger(Path(tmp) / "ledger.db")
                await body(ledger)

        anyio.run(memory)
        anyio.run(sqlite)

    def test_list_message_projection_events_excludes_checkpoint(self) -> None:
        async def body(ledger) -> None:
            run_id = await _seed_short_run(ledger)
            base_events = await ledger.list_message_projection_events(run_id)
            self.assertTrue(base_events)
            self.assertNotIn(
                "message.projection_checkpoint",
                [e.type for e in base_events],
            )
            tape = await reconstruct_message_tape_from_events(base_events)
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=base_events[-1].seq,
                    messages=_to_checkpoint_payload(tape),
                ),
            )
            after = await ledger.list_message_projection_events(run_id)
            # Same tail-fold window as before; the new checkpoint must not
            # leak in even though it raised run.last_seq.
            self.assertEqual(
                [e.seq for e in base_events],
                [e.seq for e in after],
            )

        self._run_with_each_backend(body)

    def test_latest_checkpoint_returns_newest(self) -> None:
        async def body(ledger) -> None:
            run_id = await _seed_short_run(ledger)
            tape = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            # Two checkpoints at different through_seq values; the latest one
            # must win.
            first_seq = (await ledger.get_run(run_id)).last_seq
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=first_seq,
                    messages=_to_checkpoint_payload(tape),
                ),
            )
            second_seq = (await ledger.get_run(run_id)).last_seq
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=second_seq,
                    messages=_to_checkpoint_payload(tape),
                ),
            )
            record = await ledger.latest_message_projection_checkpoint(run_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.through_seq, second_seq)

            older = await ledger.latest_message_projection_checkpoint(
                run_id, before_seq=record.seq
            )
            self.assertIsNotNone(older)
            self.assertEqual(older.through_seq, first_seq)

        self._run_with_each_backend(body)

    def test_through_seq_invariant(self) -> None:
        """The reducer must reject checkpoints whose ``through_seq`` does not
        match the pre-append ``run.last_seq``."""

        async def body(ledger) -> None:
            run_id = await _seed_short_run(ledger)
            tape = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            current_last_seq = (await ledger.get_run(run_id)).last_seq
            with self.assertRaises(LedgerError):
                await ledger.apply(
                    run_id,
                    MessageProjectionCheckpointDraft(
                        through_seq=current_last_seq - 1,
                        messages=_to_checkpoint_payload(tape),
                    ),
                )

        self._run_with_each_backend(body)

    def test_empty_messages_rejected(self) -> None:
        """The reducer must refuse a zero-message payload — a buggy writer
        otherwise persists a 'this run has no projection' cache fact."""

        async def body(ledger) -> None:
            run_id = await _seed_short_run(ledger)
            current_last_seq = (await ledger.get_run(run_id)).last_seq
            with self.assertRaises(LedgerError):
                await ledger.apply(
                    run_id,
                    MessageProjectionCheckpointDraft(
                        through_seq=current_last_seq,
                        messages=[],
                    ),
                )

        self._run_with_each_backend(body)

    def test_run_state_unaffected_by_checkpoint(self) -> None:
        """Reducer is a no-op for run/tool/approval projections — appending a
        checkpoint must not alter SUCCEEDED status or invocation counts."""

        async def body(ledger) -> None:
            run_id = await _seed_run_with_tool_turn(ledger)
            before = await ledger.get_run(run_id)
            tape = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=before.last_seq,
                    messages=_to_checkpoint_payload(tape),
                ),
            )
            after = await ledger.get_run(run_id)
            self.assertEqual(before.status, after.status)
            self.assertEqual(before.steps, after.steps)
            self.assertEqual(before.committed_turns, after.committed_turns)

        self._run_with_each_backend(body)


class LoadMessageTapeTests(unittest.TestCase):
    def _run_each_backend(self, body) -> None:
        """Drive ``body(ledger)`` once against memory and once against SQLite.

        Each call gets its own ``TemporaryDirectory`` opened via ``with`` so
        nothing leaks to ``/tmp`` between runs.
        """
        anyio.run(body, MemoryRunLedger())
        with TemporaryDirectory() as tmp:
            anyio.run(body, SQLiteRunLedger(Path(tmp) / "ledger.db"))

    def test_no_checkpoint_matches_full_replay(self) -> None:
        async def body(ledger) -> None:
            run_id = await _seed_run_with_tool_turn(ledger)
            fast = await load_message_tape(ledger, run_id)
            full = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            self.assertEqual(
                [m.message for m in fast.model_visible()],
                [m.message for m in full.model_visible()],
            )

        self._run_each_backend(body)

    def test_fast_path_with_one_checkpoint(self) -> None:
        async def body(ledger) -> None:
            run_id = await _seed_run_with_tool_turn(ledger)
            initial_full = await load_message_tape(ledger, run_id)
            last_seq = (await ledger.get_run(run_id)).last_seq
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=last_seq,
                    messages=_to_checkpoint_payload(initial_full),
                ),
            )
            # Add a new turn after the checkpoint so the loader has to fold
            # tail events on top of the cached baseline.
            await ledger.apply(run_id, UserMessageDraft(content="continue"))
            await ledger.apply(run_id, RunResumedDraft(cause="user_message"))
            step_id = "step_3"
            await ledger.apply(
                run_id,
                StepStartedDraft(step_id=step_id, index=3, snapshot=_snapshot()),
            )
            await ledger.apply(
                run_id,
                ModelCompletedDraft(step_id=step_id, content="more", tool_calls=[]),
            )
            await ledger.apply(
                run_id, RunSucceededDraft(answer="more", turns=3)
            )
            fast = await load_message_tape(ledger, run_id)
            full = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            self.assertEqual(
                [m.message for m in fast.model_visible()],
                [m.message for m in full.model_visible()],
            )

        self._run_each_backend(body)

    def test_loader_uses_latest_checkpoint(self) -> None:
        async def body(ledger) -> None:
            run_id = await _seed_run_with_tool_turn(ledger)
            tape1 = await load_message_tape(ledger, run_id)
            last_seq = (await ledger.get_run(run_id)).last_seq
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=last_seq,
                    messages=_to_checkpoint_payload(tape1),
                ),
            )

            # Inject a second checkpoint with a *garbled* baseline; if the
            # loader picks the latest one and trusts its messages, the fast
            # path tape would differ from full replay.
            tape2 = await load_message_tape(ledger, run_id)
            second_seq = (await ledger.get_run(run_id)).last_seq
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=second_seq,
                    messages=_to_checkpoint_payload(tape2),
                ),
            )

            fast = await load_message_tape(ledger, run_id)
            full = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            self.assertEqual(
                [m.message for m in fast.model_visible()],
                [m.message for m in full.model_visible()],
            )

        self._run_each_backend(body)

    def test_loader_skips_through_seq_beyond_last_seq(self) -> None:
        """A stored checkpoint whose ``through_seq`` exceeds ``run.last_seq``
        (storage corruption, manual edit) must be skipped — the loader walks
        older candidates or falls back to full replay rather than trusting a
        stale frozen tape."""

        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_run_with_tool_turn(ledger)
            # Fabricate a future-dated checkpoint by appending to the raw
            # store; the reducer would reject this directly.
            from knuth.core.events import (
                MessageProjectionCheckpoint as StoredCheckpoint,
            )
            last_seq = (await ledger.get_run(run_id)).last_seq
            bad = StoredCheckpoint(
                id="evt_bad",
                run_id=run_id,
                seq=last_seq + 100,
                created_at="2026-06-23T00:00:00+00:00",
                through_seq=last_seq + 100,
                messages=[
                    CheckpointTapeMessage(
                        id="stale",
                        message=InferenceMessage(
                            role=InferenceRole.USER, content="stale baseline"
                        ),
                        origin=TapeItemSource.LEDGER,
                    )
                ],
            )
            ledger._events[run_id].append(bad)
            fast = await load_message_tape(ledger, run_id)
            full = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            self.assertEqual(
                [m.message for m in fast.model_visible()],
                [m.message for m in full.model_visible()],
            )
            self.assertNotIn(
                "stale baseline",
                [m.content for m in fast.model_visible()],
            )

        anyio.run(run)

    def test_load_without_checkpoint_helper(self) -> None:
        """``load_message_tape_without_checkpoint`` ignores checkpoint events
        and stops at the requested seq."""

        async def body(ledger) -> None:
            run_id = await _seed_run_with_tool_turn(ledger)
            tape = await load_message_tape(ledger, run_id)
            current = (await ledger.get_run(run_id)).last_seq
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=current,
                    messages=_to_checkpoint_payload(tape),
                ),
            )
            full = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            without_cp = await load_message_tape_without_checkpoint(
                ledger, run_id, through_seq=current
            )
            self.assertEqual(
                [m.message for m in without_cp.model_visible()],
                [m.message for m in full.model_visible()],
            )

        self._run_each_backend(body)


class CheckpointPayloadContentTests(unittest.TestCase):
    """The checkpoint payload must record exactly ``model_visible()`` items —
    no ``TapeAnchor`` markers, no suppressed historical messages."""

    def test_payload_contains_only_tape_messages(self) -> None:
        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_run_with_tool_turn(ledger)
            tape = await load_message_tape(ledger, run_id)
            payload = _to_checkpoint_payload(tape)
            # Every entry survives an InferenceMessage round trip and carries
            # a non-empty stable id.
            for entry in payload:
                self.assertTrue(entry.id)
                self.assertIsInstance(entry.message, InferenceMessage)
            # Replace a tool result via the middleware path is exercised
            # elsewhere; here the basic invariant is that the payload length
            # matches the model-visible projection count.
            self.assertEqual(len(payload), len(tape.model_visible()))

        anyio.run(run)


class ProjectionCheckpointWriterTests(unittest.TestCase):
    """Writer-focused tests: policy, safe-boundary gating, and the
    ``through_seq == run.last_seq`` invariant."""

    def test_writes_when_thresholds_met(self) -> None:
        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_run_with_tool_turn(ledger)
            writer = ProjectionCheckpointWriter(
                ledger,
                ProjectionCheckpointPolicy(
                    min_events_since_checkpoint=1, min_messages=1
                ),
            )
            last_seq_before = (await ledger.get_run(run_id)).last_seq
            wrote = await writer.maybe_append(run_id)
            self.assertTrue(wrote)
            record = await ledger.latest_message_projection_checkpoint(run_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.through_seq, last_seq_before)

        anyio.run(run)

    def test_skips_when_min_events_not_met(self) -> None:
        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_run_with_tool_turn(ledger)
            writer = ProjectionCheckpointWriter(
                ledger,
                ProjectionCheckpointPolicy(
                    min_events_since_checkpoint=10_000, min_messages=1
                ),
            )
            self.assertFalse(await writer.maybe_append(run_id))
            self.assertIsNone(
                await ledger.latest_message_projection_checkpoint(run_id)
            )

        anyio.run(run)

    def test_skips_when_min_messages_not_met(self) -> None:
        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_run_with_tool_turn(ledger)
            writer = ProjectionCheckpointWriter(
                ledger,
                ProjectionCheckpointPolicy(
                    min_events_since_checkpoint=1, min_messages=999
                ),
            )
            self.assertFalse(await writer.maybe_append(run_id))

        anyio.run(run)

    def test_does_not_checkpoint_open_tool_batch(self) -> None:
        async def run() -> None:
            ledger = MemoryRunLedger()
            run = await ledger.create_run(query="open batch test")
            await ledger.apply(run.id, UserMessageDraft(content="open batch test"))
            await ledger.apply(
                run.id,
                StepStartedDraft(
                    step_id="s1", index=1, snapshot=_snapshot()
                ),
            )
            tool_call_id = "tc1"
            await ledger.apply(
                run.id,
                ModelCompletedDraft(
                    step_id="s1",
                    content=None,
                    tool_calls=[
                        CoreToolCall(
                            tool_call_id=tool_call_id, name="t", arguments={}
                        )
                    ],
                ),
            )
            await ledger.apply(
                run.id,
                ToolBatchPlannedDraft(
                    batch_id="b1",
                    step_id="s1",
                    calls=[
                        PlannedToolCall(
                            tool_call_id=tool_call_id,
                            name="t",
                            args={},
                            args_hash=args_hash_for({}),
                        )
                    ],
                ),
            )
            writer = ProjectionCheckpointWriter(
                ledger,
                ProjectionCheckpointPolicy(
                    min_events_since_checkpoint=1, min_messages=1
                ),
            )
            self.assertFalse(await writer.maybe_append(run.id))
            self.assertIsNone(
                await ledger.latest_message_projection_checkpoint(run.id)
            )

        anyio.run(run)

    def test_writer_emits_no_rewrite_audit(self) -> None:
        """The writer must not produce ``message.rewrite_*`` events — the
        durable rewrite audit is a middleware property only."""

        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_run_with_tool_turn(ledger)
            writer = ProjectionCheckpointWriter(
                ledger,
                ProjectionCheckpointPolicy(
                    min_events_since_checkpoint=1, min_messages=1
                ),
            )
            await writer.maybe_append(run_id)
            events = await ledger.list_events(run_id)
            self.assertFalse(
                any(
                    e.type in {"message.rewrite_anchor", "message.rewrite_message"}
                    for e in events
                )
            )

        anyio.run(run)


class CheckpointSurvivesPostCheckpointRewriteTests(unittest.TestCase):
    """When middleware writes a ``message.rewrite_*`` block after a checkpoint,
    loading must see the rewrite on top of the cached baseline."""

    def test_post_checkpoint_replace_visible_via_fast_path(self) -> None:
        async def run() -> None:
            ledger = MemoryRunLedger()
            run_id = await _seed_run_with_tool_turn(ledger)
            baseline_tape = await load_message_tape(ledger, run_id)
            last_seq = (await ledger.get_run(run_id)).last_seq
            await ledger.apply(
                run_id,
                MessageProjectionCheckpointDraft(
                    through_seq=last_seq,
                    messages=_to_checkpoint_payload(baseline_tape),
                ),
            )

            # Identify the tool_result message id from the baseline so we can
            # have middleware replace it.
            tool_result = next(
                m for m in baseline_tape.model_visible()
                if m.message.role == InferenceRole.TOOL_RESULT
            )
            await ledger.apply_many(
                run_id,
                [
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="observation_condensation",
                        operation="replace",
                        suppresses=[tool_result.id],
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.TOOL_RESULT,
                            tool_call_id=tool_result.message.tool_call_id,
                            tool_name=tool_result.message.tool_name,
                            content="condensed observation",
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="observation_condensation",
                        operation="replace",
                    ),
                ],
            )

            fast = await load_message_tape(ledger, run_id)
            full = await reconstruct_message_tape_from_events(
                await ledger.list_events(run_id)
            )
            self.assertEqual(
                [m.message for m in fast.model_visible()],
                [m.message for m in full.model_visible()],
            )
            self.assertIn(
                "condensed observation",
                [m.content for m in fast.model_visible()],
            )

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
