import json
import tempfile
import unittest
import sqlite3
from pathlib import Path

import anyio

from knuth.core.events import (
    InferenceContentDelta,
    InferenceGenerationCompleted,
    InferenceToolCallDelta,
    InferenceToolCallStarted,
    ModelReasoningDeltaDraft,
    RunCreatedDraft,
    emit_transient_runtime_event,
)
from knuth.core.invocations import (
    ToolCallDecision,
    ToolEffect,
    ToolInvocationStatus,
    ToolRisk,
    args_hash_for,
)
from knuth.core.messages import (
    InferenceMessage,
    InferenceRole,
    SystemSection,
    SystemSectionSource,
    ToolCall as CoreToolCall,
)
from knuth.core.runtime_events import (
    ApprovalRequestedDraft,
    ApprovalResolvedDraft,
    ContextSnapshot,
    MessageRewriteAnchorDraft,
    MessageRewriteMessageDraft,
    ModelCompletedDraft,
    PlannedToolCall,
    RunResumedDraft,
    RunSucceededDraft,
    StepStartedDraft,
    InsertPosition,
    ToolBatchClosedDraft,
    ToolBatchPlannedDraft,
    ToolInvocationCompletedDraft,
    ToolInvocationStartedDraft,
    ToolProposedDraft,
    UserMessageDraft,
    VerificationFailedDraft,
)
from knuth.core.skills import SkillSource
from knuth.core.tools import ToolResult
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig
from knuth_runtime import (
    AgentRuntime,
    CrashRecoveryReport,
    DebugEventSink,
    FilesystemArtifactStore,
    LedgerError,
    MemoryRunLedger,
    ContextCompactionMiddleware,
    InsertPatch,
    MessageMiddleware,
    MessageMiddlewareCheckpoint,
    MessageMiddlewareContext,
    MessageMiddlewareRunner,
    RegexSecretRedactor,
    SkillReminderMiddleware,
    SkillRuntimeConfig,
    SQLiteRunLedger,
    build_memory_runtime,
    build_sqlite_runtime,
)
from knuth_runtime.context import (
    ContextBuilder,
    StaticSectionProvider,
    TapeMessage,
    assemble_preamble,
    project_messages_from_events,
    raw_ledger_messages_from_events,
)
from knuth_runtime.loop import EMPTY_ANSWER_FEEDBACK
from knuth_runtime.observation import RuntimeEventInterest, RuntimeObservationError
from knuth_runtime.policy import PolicyEngine
from knuth_runtime.services import RuntimeServices
from knuth_toold import ToolBroker, create_default_registry
from knuth_toold.base import ToolManifest, ToolRuntimeContext
from knuth_toold.process_output import parse_tagged_process_output
from knuth_toold.skills import SkillManager, SkillRoot


def _snapshot() -> ContextSnapshot:
    return ContextSnapshot(
        messages_hash="m",
        tools_hash="t",
        preamble_hash="p",
        model_config_hash="c",
        message_count=1,
        tool_count=0,
    )


class RunLedgerTests(unittest.TestCase):
    def test_every_durable_draft_has_a_registered_reducer(self) -> None:
        from typing import get_args

        from knuth.core.runtime_events import DurableRuntimeEventDraft
        from knuth_runtime.ledger import _REDUCERS

        unhandled = [
            draft_cls.__name__
            for draft_cls in get_args(DurableRuntimeEventDraft)
            # run.created bootstraps the aggregate inside reduce_run_event.
            if draft_cls is not RunCreatedDraft and draft_cls not in _REDUCERS
        ]
        self.assertEqual(unhandled, [])

    def test_apply_stores_typed_event_and_updates_run_projection(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("hello")
            events = await ledger.list_events(run.id)
            return run, events

        run, events = anyio.run(scenario)
        self.assertEqual(run.status, RunStatus.CREATED)
        self.assertEqual(run.last_seq, 1)
        self.assertEqual(events[0].type, "run.created")
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[0].query, "hello")

    def test_create_run_accepts_caller_supplied_id(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("hello", run_id="run_manual")
            fetched = await ledger.get_run("run_manual")
            return run, fetched

        run, fetched = anyio.run(scenario)
        self.assertEqual(run.id, "run_manual")
        self.assertEqual(fetched.query, "hello")

    def test_message_rewrite_replace_suppresses_target_on_replay(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("hello")
            await ledger.apply(run.id, UserMessageDraft(content="raw message"))
            await ledger.apply_many(
                run.id,
                [
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="context_compaction",
                        operation="replace",
                        suppresses=["m:2"],
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.USER,
                            content="Earlier context summary: raw message",
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="context_compaction",
                        operation="replace",
                    ),
                ],
            )
            events = await ledger.list_events(run.id)
            messages = await project_messages_from_events(events)
            await ledger.refold()
            return messages, await ledger.list_events(run.id)

        messages, events_after_refold = anyio.run(scenario)
        self.assertEqual([message.content for message in messages], [
            "Earlier context summary: raw message"
        ])
        self.assertEqual(
            [event.type for event in events_after_refold][-3:],
            [
                "message.rewrite_anchor",
                "message.rewrite_message",
                "message.rewrite_anchor",
            ],
        )
        self.assertEqual(events_after_refold[-3].rewrite_id, "rw:3")
        self.assertEqual(events_after_refold[-2].rewrite_id, "rw:3")
        self.assertEqual(events_after_refold[-2].message_id, "rw:3#0")

    def test_message_rewrite_preserves_inference_message_name(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("hello")
            await ledger.apply(run.id, UserMessageDraft(content="raw message"))
            await ledger.apply_many(
                run.id,
                [
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="context_compaction",
                        operation="replace",
                        suppresses=["m:2"],
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.USER,
                            content="named replacement",
                            name="context_compaction",
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="context_compaction",
                        operation="replace",
                    ),
                ],
            )
            return await project_messages_from_events(await ledger.list_events(run.id))

        messages = anyio.run(scenario)
        self.assertEqual(messages[0].content, "named replacement")
        self.assertEqual(messages[0].name, "context_compaction")

    def test_message_rewrite_rejects_conflicting_replace(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("hello")
            await ledger.apply(run.id, UserMessageDraft(content="raw message"))
            patch = [
                MessageRewriteAnchorDraft(
                    kind="begin",
                    middleware="context_compaction",
                    operation="replace",
                    suppresses=["m:2"],
                ),
                MessageRewriteMessageDraft(
                    message=InferenceMessage(
                        role=InferenceRole.USER,
                        content="summary",
                    ),
                ),
                MessageRewriteAnchorDraft(
                    kind="end",
                    middleware="context_compaction",
                    operation="replace",
                ),
            ]
            await ledger.apply_many(run.id, patch)
            with self.assertRaises(LedgerError):
                await ledger.apply_many(
                    run.id,
                    [
                        MessageRewriteAnchorDraft(
                            kind="begin",
                            middleware="context_compaction",
                            operation="replace",
                            suppresses=["m:2"],
                        ),
                        MessageRewriteMessageDraft(
                            message=InferenceMessage(
                                role=InferenceRole.USER,
                                content="other summary",
                            ),
                        ),
                        MessageRewriteAnchorDraft(
                            kind="end",
                            middleware="context_compaction",
                            operation="replace",
                        ),
                    ],
                )

            anyio.run(scenario)

    def test_message_rewrite_insert_position_preserves_intermediate_projection(
        self,
    ) -> None:
        async def scenario():
            ledger = MemoryRunLedger()
            run = await ledger.create_run("hello")
            await ledger.apply(run.id, UserMessageDraft(content="left"))
            await ledger.apply(run.id, UserMessageDraft(content="right"))
            await ledger.apply_many(
                run.id,
                [
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="agents_md",
                        operation="insert",
                        position=InsertPosition(kind="before", target_id="m:3"),
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.USER, content="middle"
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="agents_md",
                        operation="insert",
                    ),
                ],
            )
            await ledger.apply_many(
                run.id,
                [
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="context_compaction",
                        operation="replace",
                        suppresses=["m:2", "m:3"],
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.USER, content="summary"
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="context_compaction",
                        operation="replace",
                    ),
                ],
            )
            return await project_messages_from_events(await ledger.list_events(run.id))

        messages = anyio.run(scenario)
        self.assertEqual([message.content for message in messages], ["summary", "middle"])

    def test_message_rewrite_insert_can_anchor_to_replaced_target_in_same_batch(
        self,
    ) -> None:
        async def scenario():
            ledger = MemoryRunLedger()
            run = await ledger.create_run("hello")
            await ledger.apply(run.id, UserMessageDraft(content="left"))
            await ledger.apply_many(
                run.id,
                [
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="inserter",
                        operation="insert",
                        position=InsertPosition(kind="before", target_id="m:2"),
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.USER,
                            content="inserted before original",
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="inserter",
                        operation="insert",
                    ),
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="replacer",
                        operation="replace",
                        suppresses=["m:2"],
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.USER,
                            content="replacement",
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="replacer",
                        operation="replace",
                    ),
                ],
            )
            return await project_messages_from_events(await ledger.list_events(run.id))

        messages = anyio.run(scenario)
        self.assertEqual(
            [message.content for message in messages],
            ["inserted before original", "replacement"],
        )

    def test_message_rewrite_events_must_be_atomic(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("hello")
            await ledger.apply(run.id, UserMessageDraft(content="raw message"))
            with self.assertRaises(LedgerError):
                await ledger.apply(
                    run.id,
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="context_compaction",
                        operation="replace",
                        suppresses=["m:2"],
                    ),
                )

        anyio.run(scenario)

    def test_sqlite_ledger_round_trips_events_and_projections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = SQLiteRunLedger(Path(temp_dir, "knuth.db"))

            async def scenario():
                run = await ledger.create_run("hello")
                await ledger.apply(run.id, UserMessageDraft(content="hi"))
                events = await ledger.list_events(run.id)
                state = await ledger.run_state(run.id)
                return events, state

            events, state = anyio.run(scenario)
        self.assertEqual([event.type for event in events], ["run.created", "user.message"])
        self.assertEqual(state.run.last_seq, 2)
        self.assertIsNone(state.open_batch)

    def test_sqlite_ledger_rejects_legacy_unversioned_database_with_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir, "knuth.db")
            ledger = SQLiteRunLedger(db_path)

            async def scenario():
                await ledger.create_run("hello")

            anyio.run(scenario)
            with sqlite3.connect(db_path) as conn:
                conn.execute("pragma user_version = 0")

            with self.assertRaisesRegex(RuntimeError, "breaking ledger schema"):
                SQLiteRunLedger(db_path)

    def test_memory_and_sqlite_ledgers_project_the_same_state(self) -> None:
        # Both implementations share the apply orchestration in _LedgerMixin;
        # the same event sequence must yield the same projections.
        def drive(ledger):
            async def scenario():
                run = await ledger.create_run("hello")
                await ledger.apply(
                    run.id,
                    StepStartedDraft(step_id="step-1", index=1, snapshot=_snapshot()),
                )
                call = CoreToolCall(tool_call_id="call-1", name="read_file", arguments={"path": "x"})
                await ledger.apply(
                    run.id,
                    ModelCompletedDraft(step_id="step-1", tool_calls=[call]),
                )
                await ledger.apply(
                    run.id,
                    ToolBatchPlannedDraft(
                        batch_id="batch-1",
                        step_id="step-1",
                        calls=[
                            PlannedToolCall(
                                tool_call_id="call-1",
                                name="read_file",
                                args={"path": "x"},
                                args_hash=args_hash_for({"path": "x"}),
                            )
                        ],
                    ),
                )
                events = await ledger.list_events(run.id)
                state = await ledger.run_state(run.id)
                return [event.type for event in events], state

            return anyio.run(scenario)

        with tempfile.TemporaryDirectory() as temp_dir:
            event_types_mem, state_mem = drive(MemoryRunLedger())
            event_types_sql, state_sql = drive(SQLiteRunLedger(Path(temp_dir, "k.db")))

        self.assertEqual(event_types_mem, event_types_sql)
        for state in (state_mem, state_sql):
            self.assertEqual(state.run.status, RunStatus.RUNNING)
            self.assertEqual(state.run.open_batch_id, "batch-1")
            self.assertEqual(state.open_batch.step_id, "step-1")
            self.assertEqual(
                [inv.tool_call_id for inv in state.open_batch.invocations],
                ["call-1"],
            )
        self.assertEqual(state_mem.run.last_seq, state_sql.run.last_seq)

    def test_sqlite_ledger_rejects_legacy_schema(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir, "knuth.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table runs (
                      id text primary key,
                      status text not null,
                      query text not null,
                      created_at text not null,
                      updated_at text not null,
                      data_json text not null
                    )
                    """
                )

            with self.assertRaisesRegex(RuntimeError, "breaking ledger schema"):
                SQLiteRunLedger(db_path)

    def test_step_started_is_rejected_while_a_batch_is_open(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("q")
            await ledger.apply(run.id, UserMessageDraft(content="q"))
            await ledger.apply(
                run.id, StepStartedDraft(step_id="s1", index=1, snapshot=_snapshot())
            )
            await ledger.apply(
                run.id,
                ModelCompletedDraft(
                    step_id="s1",
                    content=None,
                    tool_calls=[CoreToolCall(tool_call_id="c1", name="read_file", arguments={"path": "x"})],
                ),
            )
            await ledger.apply(
                run.id,
                ToolBatchPlannedDraft(
                    batch_id="b1",
                    step_id="s1",
                    calls=[
                        PlannedToolCall(
                            tool_call_id="c1",
                            index=0,
                            name="read_file",
                            args={"path": "x"},
                            args_hash=args_hash_for({"path": "x"}),
                        )
                    ],
                ),
            )
            with self.assertRaisesRegex(LedgerError, "open tool batch"):
                await ledger.apply(
                    run.id,
                    StepStartedDraft(step_id="s2", index=2, snapshot=_snapshot()),
                )

        anyio.run(scenario)

    def test_batch_planned_must_match_latest_model_tool_calls(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("q")
            await ledger.apply(run.id, UserMessageDraft(content="q"))
            await ledger.apply(
                run.id, StepStartedDraft(step_id="s1", index=1, snapshot=_snapshot())
            )
            await ledger.apply(
                run.id,
                ModelCompletedDraft(
                    step_id="s1",
                    tool_calls=[CoreToolCall(tool_call_id="c1", name="read_file", arguments={})],
                ),
            )
            with self.assertRaisesRegex(LedgerError, "do not match"):
                await ledger.apply(
                    run.id,
                    ToolBatchPlannedDraft(
                        batch_id="b1",
                        step_id="s1",
                        calls=[
                            PlannedToolCall(
                                tool_call_id="other",
                                index=0,
                                name="read_file",
                                args={},
                                args_hash=args_hash_for({}),
                            )
                        ],
                    ),
                )

        anyio.run(scenario)

    def test_resume_is_rejected_while_approvals_are_pending(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run_id = await _setup_awaiting_approval(ledger)
            with self.assertRaisesRegex(LedgerError, "pending approvals"):
                await ledger.apply(run_id, RunResumedDraft(cause="user_resume"))

        anyio.run(scenario)

    def test_approval_request_binds_to_frozen_args_hash(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run_id = await _setup_proposed_batch(
                ledger, decision=ToolCallDecision.REQUIRES_APPROVAL
            )
            from knuth.core.runtime_events import ApprovalRequestedDraft

            with self.assertRaisesRegex(LedgerError, "args_hash"):
                await ledger.apply(
                    run_id,
                    ApprovalRequestedDraft(
                        approval_id="appr-x",
                        tool_call_id="c1",
                        args_hash="not-the-frozen-hash",
                        title="t",
                        reason="r",
                        risk="low",
                    ),
                )

        anyio.run(scenario)

    def test_verification_failed_requires_feedback(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("q")
            await ledger.apply(run.id, UserMessageDraft(content="q"))
            await ledger.apply(
                run.id, StepStartedDraft(step_id="s1", index=1, snapshot=_snapshot())
            )
            with self.assertRaisesRegex(LedgerError, "feedback"):
                await ledger.apply(
                    run.id,
                    VerificationFailedDraft(reason="empty", feedback="   "),
                )

        anyio.run(scenario)

    def test_rejected_event_persists_nothing(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run = await ledger.create_run("q")
            await ledger.apply(run.id, UserMessageDraft(content="q"))
            await ledger.apply(
                run.id, StepStartedDraft(step_id="s1", index=1, snapshot=_snapshot())
            )
            before = await ledger.list_events(run.id)
            with self.assertRaises(LedgerError):
                await ledger.apply(
                    run.id,
                    VerificationFailedDraft(reason="empty", feedback="   "),
                )
            after = await ledger.list_events(run.id)
            return before, after

        before, after = anyio.run(scenario)
        self.assertEqual(len(before), len(after))


class RuntimeBuilderToolTests(unittest.TestCase):
    def test_memory_runtime_accepts_host_provider_without_default_tools(self) -> None:
        class HostReadFileTool:
            @property
            def manifest(self) -> ToolManifest:
                return ToolManifest(
                    name="read_file",
                    description="host read_file override",
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                )

            async def invoke(self, invocation, ctx: ToolRuntimeContext) -> ToolResult:
                return ToolResult.success(content="host")

        class HostToolProvider:
            name = "host"

            def __init__(self) -> None:
                self._tool = HostReadFileTool()

            async def list_tools(self) -> list[ToolManifest]:
                return [self._tool.manifest]

            async def call_tool(
                self, invocation, ctx: ToolRuntimeContext
            ) -> ToolResult:
                return await self._tool.invoke(invocation, ctx)

        runtime = build_memory_runtime(
            inference_client=None,
            inference_config=InferenceConfig(),
            tool_providers=[HostToolProvider()],
            include_default_tools=False,
        )

        tools = anyio.run(runtime.tools)
        by_name = {
            item["function"]["name"]: item["function"]["description"]
            for item in tools
        }

        self.assertEqual(by_name["read_file"], "host read_file override")
        self.assertNotIn("write_file", by_name)


class _SecretRedactor:
    def redact_event(self, draft):
        if hasattr(draft, "content") and isinstance(draft.content, str):
            return draft.model_copy(
                update={"content": draft.content.replace("s3cret", "[redacted]")}
            )
        return draft


class RedactionTests(unittest.TestCase):
    def test_redaction_happens_before_append(self) -> None:
        ledger = MemoryRunLedger(redactor=_SecretRedactor())

        async def scenario():
            run = await ledger.create_run("q")
            await ledger.apply(run.id, UserMessageDraft(content="key=s3cret"))
            return await ledger.list_events(run.id)

        events = anyio.run(scenario)
        self.assertEqual(events[-1].content, "key=[redacted]")

    def test_default_redactor_masks_known_secret_shapes(self) -> None:
        redactor = RegexSecretRedactor()
        masked = redactor.redact_text(
            "openai sk-abcdefghijklmnopqrstuvwxyz123456 and"
            " Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload"
            " and api_key=hunter2hunter2"
        )
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", masked)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", masked)
        self.assertNotIn("hunter2hunter2", masked)
        self.assertIn("[REDACTED:openai_key]", masked)
        self.assertIn("[REDACTED:bearer_token]", masked)
        self.assertIn("[REDACTED:credential]", masked)

    def test_secret_tool_args_are_redacted_and_rehashed_before_append(self) -> None:
        ledger = MemoryRunLedger(redactor=RegexSecretRedactor())
        secret_args = {"path": "x.txt", "password": "supersecretvalue"}

        async def scenario():
            run_id = await _setup_proposed_batch(
                ledger, decision=ToolCallDecision.ALLOWED, args=secret_args
            )
            events = await ledger.list_events(run_id)
            invocation = await ledger.get_invocation("c1")
            return events, invocation

        events, invocation = anyio.run(scenario)
        planned = next(e for e in events if e.type == "tool.batch_planned")
        self.assertEqual(
            planned.calls[0].args["password"], "[REDACTED:sensitive_key]"
        )
        # The hash binds approvals to the args as frozen in the ledger: it
        # must cover the redacted form, or the aggregate would have rejected
        # the event.
        self.assertEqual(planned.calls[0].args_hash, args_hash_for(planned.calls[0].args))
        self.assertEqual(invocation.args["password"], "[REDACTED:sensitive_key]")

    def test_approval_preview_is_redacted_before_append(self) -> None:
        ledger = MemoryRunLedger(redactor=RegexSecretRedactor())
        secret_args = {"path": "x.txt", "password": "supersecretvalue"}

        async def scenario():
            run_id = await _setup_proposed_batch(
                ledger,
                decision=ToolCallDecision.REQUIRES_APPROVAL,
                args=secret_args,
            )
            invocation = await ledger.get_invocation("c1")
            await ledger.apply(
                run_id,
                ApprovalRequestedDraft(
                    approval_id=f"appr_{run_id}_c1",
                    tool_call_id="c1",
                    args_hash=invocation.args_hash,
                    title="t",
                    reason="r",
                    risk="medium",
                    approval_preview={"tool": "write_file", "args": secret_args},
                ),
            )
            return await ledger.get_approval(f"appr_{run_id}_c1")

        approval = anyio.run(scenario)
        self.assertEqual(
            approval.approval_preview["args"]["password"],
            "[REDACTED:sensitive_key]",
        )

    def test_artifact_content_is_redacted_before_write(self) -> None:
        ledger = MemoryRunLedger(redactor=RegexSecretRedactor())

        async def scenario():
            run = await ledger.create_run("q")
            store = FilesystemArtifactStore(
                Path(tempfile.mkdtemp()),
                redactor=RegexSecretRedactor(),
            )
            artifact = await store.put(
                run.id,
                "token sk-abcdefghijklmnopqrstuvwxyz123456",
                kind="tool_observation",
                ext=".txt",
            )
            return await store.read_text(run.id, artifact.id)

        text = anyio.run(scenario)
        self.assertEqual(text, "token [REDACTED:openai_key]")

    def test_context_redact_stage_masks_message_content(self) -> None:
        from knuth_runtime.context import ContextView, RunContext

        redactor = RegexSecretRedactor()
        view = ContextView(
            run_id="r1",
            messages=[
                InferenceMessage(
                    role=InferenceRole.SYSTEM,
                    content="Use api_key=hunter2hunter2 for the backend.",
                )
            ],
            tools=[],
        )

        redacted = anyio.run(redactor.redact, RunContext(run_id="r1"), view)
        self.assertEqual(
            redacted.messages[0].content,
            "Use api_key=[REDACTED:credential] for the backend.",
        )


async def _drive_full_run(ledger) -> str:
    """A complete run: approval round-trip, execution, batch close, success."""
    run_id = await _setup_awaiting_approval(ledger)
    await ledger.apply(
        run_id,
        ApprovalResolvedDraft(
            approval_id=f"appr_{run_id}_c1", resolution="approved"
        ),
    )
    await ledger.apply(run_id, RunResumedDraft(cause="user_resume"))
    await ledger.apply(
        run_id,
        ToolInvocationStartedDraft(
            tool_call_id="c1", attempt=1
        ),
    )
    await ledger.apply(
        run_id,
        ToolInvocationCompletedDraft(
            tool_call_id="c1",
            tool_name="write_file",
            outcome="succeeded",
            observation="ok",
        ),
    )
    await ledger.apply(run_id, ToolBatchClosedDraft(batch_id="b1"))
    await ledger.apply(run_id, RunSucceededDraft(answer="done", turns=1))
    return run_id


class RefoldTests(unittest.TestCase):
    def _projections(self, ledger, run_id):
        async def scenario():
            return (
                await ledger.get_run(run_id),
                await ledger.get_invocation("c1"),
                await ledger.get_approval(f"appr_{run_id}_c1"),
            )

        return anyio.run(scenario)

    def test_refold_rebuilds_identical_projections(self) -> None:
        # Projections are derived caches (design rule three): replaying the
        # event log must land on byte-identical state on both backends.
        with tempfile.TemporaryDirectory() as temp_dir:
            for ledger in (
                MemoryRunLedger(),
                SQLiteRunLedger(Path(temp_dir, "k.db")),
            ):
                with self.subTest(ledger=type(ledger).__name__):
                    run_id = anyio.run(_drive_full_run, ledger)
                    before = self._projections(ledger, run_id)
                    stats = anyio.run(ledger.refold)
                    after = self._projections(ledger, run_id)
                    self.assertEqual(stats.runs, 1)
                    self.assertGreater(stats.events, 0)
                    for original, refolded in zip(before, after, strict=True):
                        self.assertEqual(
                            original.model_dump(), refolded.model_dump()
                        )

    def test_refold_repairs_corrupted_projection(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir, "k.db")
            ledger = SQLiteRunLedger(db_path)
            run_id = anyio.run(_drive_full_run, ledger)
            with sqlite3.connect(db_path) as conn:
                conn.execute("update runs set status = 'failed'")
                conn.execute("update tool_invocations set status = 'unknown'")

            anyio.run(ledger.refold)

            run, invocation, _ = self._projections(ledger, run_id)
            self.assertEqual(run.status, RunStatus.SUCCEEDED)
            self.assertEqual(invocation.status, ToolInvocationStatus.SUCCEEDED)

    def test_tool_result_rewrite_preserves_tool_call_shape(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run_id = await _drive_full_run(ledger)
            events = await ledger.list_events(run_id)
            tool_event = next(
                event for event in events if event.type == "tool.invocation_completed"
            )
            target_id = f"m:{tool_event.seq}"
            await ledger.apply_many(
                run_id,
                [
                    MessageRewriteAnchorDraft(
                        kind="begin",
                        middleware="observation_condensation",
                        operation="replace",
                        suppresses=[target_id],
                    ),
                    MessageRewriteMessageDraft(
                        message=InferenceMessage(
                            role=InferenceRole.TOOL_RESULT,
                            tool_call_id="c1",
                            tool_name="write_file",
                            content="Observation condensed for context headroom.",
                        ),
                    ),
                    MessageRewriteAnchorDraft(
                        kind="end",
                        middleware="observation_condensation",
                        operation="replace",
                    ),
                ],
            )
            messages = await project_messages_from_events(
                await ledger.list_events(run_id)
            )
            return messages

        messages = anyio.run(scenario)
        tool_messages = [
            message
            for message in messages
            if message.role == InferenceRole.TOOL_RESULT
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].tool_call_id, "c1")
        self.assertEqual(
            tool_messages[0].content, "Observation condensed for context headroom."
        )

    def test_tool_result_rewrite_rejects_different_tool_call_id(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            run_id = await _drive_full_run(ledger)
            events = await ledger.list_events(run_id)
            tool_event = next(
                event for event in events if event.type == "tool.invocation_completed"
            )
            with self.assertRaises(LedgerError):
                await ledger.apply_many(
                    run_id,
                    [
                        MessageRewriteAnchorDraft(
                            kind="begin",
                            middleware="observation_condensation",
                            operation="replace",
                            suppresses=[f"m:{tool_event.seq}"],
                        ),
                        MessageRewriteMessageDraft(
                            message=InferenceMessage(
                                role=InferenceRole.TOOL_RESULT,
                                tool_call_id="other",
                                tool_name="write_file",
                                content="bad",
                            ),
                        ),
                        MessageRewriteAnchorDraft(
                            kind="end",
                            middleware="observation_condensation",
                            operation="replace",
                        ),
                    ],
                )

        anyio.run(scenario)


async def _setup_proposed_batch(
    ledger,
    *,
    decision: ToolCallDecision,
    effect: ToolEffect = ToolEffect.LOCAL_WRITE,
    args: dict | None = None,
    tool_name: str = "write_file",
) -> str:
    args = args if args is not None else {"path": "x.txt", "content": "hello"}
    run = await ledger.create_run("q")
    await ledger.apply(run.id, UserMessageDraft(content="q"))
    await ledger.apply(
        run.id, StepStartedDraft(step_id="s1", index=1, snapshot=_snapshot())
    )
    await ledger.apply(
        run.id,
        ModelCompletedDraft(
            step_id="s1",
            tool_calls=[CoreToolCall(tool_call_id="c1", name=tool_name, arguments=args)],
        ),
    )
    await ledger.apply(
        run.id,
        ToolBatchPlannedDraft(
            batch_id="b1",
            step_id="s1",
            calls=[
                PlannedToolCall(
                    tool_call_id="c1",
                    index=0,
                    name=tool_name,
                    args=args,
                    args_hash=args_hash_for(args),
                )
            ],
        ),
    )
    await ledger.apply(
        run.id,
        ToolProposedDraft(
            tool_call_id="c1",
            decision=decision,
            effect=effect,
            risk=ToolRisk.MEDIUM,
        ),
    )
    return run.id


async def _setup_awaiting_approval(ledger) -> str:
    from knuth.core.runtime_events import ApprovalRequestedDraft

    run_id = await _setup_proposed_batch(
        ledger, decision=ToolCallDecision.REQUIRES_APPROVAL
    )
    await ledger.apply(
        run_id,
        ApprovalRequestedDraft(
            approval_id=f"appr_{run_id}_c1",
            tool_call_id="c1",
            args_hash=args_hash_for({"path": "x.txt", "content": "hello"}),
            title="t",
            reason="r",
            risk="medium",
        ),
    )
    return run_id


class ScriptedInferenceClient:
    model = "scripted-model"

    def __init__(self, messages: list[InferenceMessage]) -> None:
        self.messages = messages
        self.calls = 0

    async def stream(self, messages, tools, config, runtime=None):
        message = self.messages[min(self.calls, len(self.messages) - 1)]
        self.calls += 1
        yield InferenceGenerationCompleted(
            generation_id=f"gen-{self.calls}",
            seq=1,
            run_id=config.run_id,
            message=message,
        )


class CapturingScriptedClient(ScriptedInferenceClient):
    """Scripts full assistant messages while recording inbound message lists."""

    def __init__(self, messages: list[InferenceMessage]) -> None:
        super().__init__(messages)
        self.captured_messages: list[list[InferenceMessage]] = []

    async def stream(self, messages, tools, config, runtime=None):
        self.captured_messages.append(list(messages))
        async for event in super().stream(messages, tools, config, runtime):
            yield event


def _build_runtime(client, section_providers=None, message_middlewares=None):
    registry = create_default_registry()
    broker = ToolBroker(registry, PolicyEngine())
    return build_memory_runtime(
        inference_client=client,
        inference_config=InferenceConfig(),
        ledger=MemoryRunLedger(),
        tool_broker=broker,
        section_providers=section_providers,
        message_middlewares=message_middlewares,
    )


class EventDrivenRuntimeTests(unittest.TestCase):
    def test_after_user_message_committed_receives_turn_start_id(self) -> None:
        class RecorderMiddleware(MessageMiddleware):
            name = "turn_start_recorder"
            priority = 1
            checkpoints = {MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED}

            def __init__(self) -> None:
                self.calls: list[tuple[str | None, list[str]]] = []

            async def process(
                self,
                ctx: MessageMiddlewareContext,
                messages,
            ):
                self.calls.append((ctx.turn_start_id, [item.id for item in messages]))
                return []

        recorder = RecorderMiddleware()
        runtime = _build_runtime(
            ScriptedInferenceClient(
                [InferenceMessage(role=InferenceRole.ASSISTANT, content="done")]
            ),
            message_middlewares=[recorder],
        )

        anyio.run(runtime.run_once, "hello")

        self.assertEqual(recorder.calls, [("m:2", ["m:2"])])

    def test_skill_invocation_records_loaded_content_as_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "example-skill"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "\n".join(
                    [
                        "---",
                        "name: example-skill",
                        "description: Use when an example skill is needed.",
                        "---",
                        "",
                        "Original skill body.",
                    ]
                ),
                encoding="utf-8",
            )
            client = CapturingScriptedClient(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content="",
                        tool_calls=[
                            CoreToolCall(
                                tool_call_id="call-skill",
                                name="skill",
                                arguments={
                                    "skill_name": "example-skill",
                                    "args": "topic=demo",
                                },
                            )
                        ],
                    ),
                    InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
                ]
            )
            runtime = build_memory_runtime(
                inference_client=client,
                inference_config=InferenceConfig(),
                include_default_tools=False,
                skill_config=SkillRuntimeConfig(
                    roots=[SkillRoot(source=SkillSource.PROJECT, path=str(root))],
                ),
            )

            turn = anyio.run(runtime.run_once, "use the example skill")
            skill_file.write_text(
                "\n".join(
                    [
                        "---",
                        "name: example-skill",
                        "description: Use when an example skill is needed.",
                        "---",
                        "",
                        "Modified skill body.",
                    ]
                ),
                encoding="utf-8",
            )
            raw_messages = anyio.run(runtime.messages, turn.run_id)

        tool_results = [
            message
            for message in raw_messages
            if message.role == InferenceRole.TOOL_RESULT
            and message.tool_name == "skill"
        ]
        self.assertEqual(len(tool_results), 1)
        self.assertIn("Original skill body.", tool_results[0].content or "")
        self.assertNotIn("Modified skill body.", tool_results[0].content or "")
        second_turn_tool_results = [
            message
            for message in client.captured_messages[1]
            if message.role == InferenceRole.TOOL_RESULT
        ]
        self.assertIn(
            "Skill arguments: topic=demo",
            second_turn_tool_results[0].content or "",
        )

    def test_skill_invocation_rejects_body_that_would_be_redacted(self) -> None:
        large_body = "x" * 5000
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "large-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: large-skill",
                        "description: Use when a large skill is needed.",
                        "---",
                        "",
                        large_body,
                    ]
                ),
                encoding="utf-8",
            )
            client = CapturingScriptedClient(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content="",
                        tool_calls=[
                            CoreToolCall(
                                tool_call_id="call-skill",
                                name="skill",
                                arguments={"skill_name": "large-skill"},
                            )
                        ],
                    ),
                    InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
                ]
            )
            runtime = build_memory_runtime(
                inference_client=client,
                inference_config=InferenceConfig(),
                include_default_tools=False,
                skill_config=SkillRuntimeConfig(
                    roots=[SkillRoot(source=SkillSource.PROJECT, path=str(root))],
                ),
            )

            turn = anyio.run(runtime.run_once, "use the large skill")
            raw_messages = anyio.run(runtime.messages, turn.run_id)

        tool_results = [
            message
            for message in raw_messages
            if message.role == InferenceRole.TOOL_RESULT
            and message.tool_name == "skill"
        ]
        self.assertEqual(len(tool_results), 1)
        self.assertIn("Tool error:", tool_results[0].content or "")
        self.assertIn("exceeding the v1 tool-result limit", tool_results[0].content or "")
        self.assertNotIn(large_body, tool_results[0].content or "")
        self.assertFalse(
            (tool_results[0].content or "").startswith(
                "Observation condensed for context headroom."
            )
        )

    def test_skill_reminder_is_durable_before_turn_user_message_and_deduplicated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "example-skill"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "\n".join(
                    [
                        "---",
                        "name: example-skill",
                        "description: Use v1.",
                        "---",
                        "",
                        "Body.",
                    ]
                ),
                encoding="utf-8",
            )
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )

            async def scenario():
                ledger = MemoryRunLedger()
                run = await ledger.create_run("hello")
                await ledger.apply(run.id, UserMessageDraft(content="hello"))
                runner = MessageMiddlewareRunner(
                    ledger,
                    [SkillReminderMiddleware(manager)],
                )
                await runner.run_checkpoint(
                    run.id,
                    MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED,
                    turn_start_id="m:2",
                )
                await runner.run_checkpoint(
                    run.id,
                    MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED,
                    turn_start_id="m:2",
                )
                events = await ledger.list_events(run.id)
                messages = await project_messages_from_events(events)
                return events, messages

            events, messages = anyio.run(scenario)

        reminders = [
            event
            for event in events
            if event.type == "message.rewrite_message"
            and event.metadata.get("semantic", {}).get("category") == "skill_reminder"
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn("- example-skill: Use v1.", reminders[0].message.content)
        self.assertEqual(
            [message.content for message in messages],
            [reminders[0].message.content, "hello"],
        )

    def test_skill_reminder_is_per_run_not_globally_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "example-skill"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "\n".join(
                    [
                        "---",
                        "name: example-skill",
                        "description: Use v1.",
                        "---",
                        "",
                        "Body.",
                    ]
                ),
                encoding="utf-8",
            )
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )

            async def scenario():
                ledger = MemoryRunLedger()
                first = await ledger.create_run("first")
                second = await ledger.create_run("second")
                await ledger.apply(first.id, UserMessageDraft(content="first"))
                await ledger.apply(second.id, UserMessageDraft(content="second"))
                runner = MessageMiddlewareRunner(
                    ledger,
                    [SkillReminderMiddleware(manager)],
                )
                await runner.run_checkpoint(
                    first.id,
                    MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED,
                    turn_start_id="m:2",
                )
                await runner.run_checkpoint(
                    second.id,
                    MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED,
                    turn_start_id="m:2",
                )
                return await ledger.list_events(first.id), await ledger.list_events(second.id)

            first_events, second_events = anyio.run(scenario)

        first_reminders = [
            event
            for event in first_events
            if event.type == "message.rewrite_message"
            and event.metadata.get("semantic", {}).get("category") == "skill_reminder"
        ]
        second_reminders = [
            event
            for event in second_events
            if event.type == "message.rewrite_message"
            and event.metadata.get("semantic", {}).get("category") == "skill_reminder"
        ]
        self.assertEqual(len(first_reminders), 1)
        self.assertEqual(len(second_reminders), 1)
        self.assertIn("- example-skill: Use v1.", first_reminders[0].message.content)
        self.assertIn("- example-skill: Use v1.", second_reminders[0].message.content)

    def test_skill_reminder_deduplicates_against_latest_digest_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "example-skill"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"

            def write_skill(description: str) -> None:
                skill_file.write_text(
                    "\n".join(
                        [
                            "---",
                            "name: example-skill",
                            f"description: {description}",
                            "---",
                            "",
                            "Body.",
                        ]
                    ),
                    encoding="utf-8",
                )

            write_skill("Use v1.")
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )

            async def scenario():
                ledger = MemoryRunLedger()
                run = await ledger.create_run("hello")
                runner = MessageMiddlewareRunner(
                    ledger,
                    [SkillReminderMiddleware(manager)],
                )

                first = await ledger.apply(run.id, UserMessageDraft(content="turn 1"))
                await runner.run_checkpoint(
                    run.id,
                    MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED,
                    turn_start_id=f"m:{first.seq}",
                )

                write_skill("Use v2.")
                manager.invalidate("skill changed to v2")
                second = await ledger.apply(run.id, UserMessageDraft(content="turn 2"))
                await runner.run_checkpoint(
                    run.id,
                    MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED,
                    turn_start_id=f"m:{second.seq}",
                )

                write_skill("Use v1.")
                manager.invalidate("skill reverted to v1")
                third = await ledger.apply(run.id, UserMessageDraft(content="turn 3"))
                await runner.run_checkpoint(
                    run.id,
                    MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED,
                    turn_start_id=f"m:{third.seq}",
                )
                return await ledger.list_events(run.id)

            events = anyio.run(scenario)

        reminders = [
            event
            for event in events
            if event.type == "message.rewrite_message"
            and event.metadata.get("semantic", {}).get("category") == "skill_reminder"
        ]
        self.assertEqual(len(reminders), 3)
        digests = [event.metadata["semantic"]["catalog_digest"] for event in reminders]
        self.assertEqual(digests[0], digests[2])
        self.assertNotEqual(digests[0], digests[1])
        self.assertIn("- example-skill: Use v1.", reminders[0].message.content)
        self.assertIn("- example-skill: Use v2.", reminders[1].message.content)
        self.assertIn("- example-skill: Use v1.", reminders[2].message.content)

    def test_runtime_executes_tool_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            fact_path = Path(workspace, "fact.txt")
            fact_path.write_text("Knuth works", encoding="utf-8")
            runtime = _build_runtime(
                ScriptedInferenceClient(
                    [
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT,
                            content="",
                            tool_calls=[
                                CoreToolCall(
                                    tool_call_id="call-1",
                                    name="read_file",
                                    arguments={"path": str(fact_path)},
                                )
                            ],
                        ),
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT,
                            content="Final answer: Knuth works",
                        ),
                    ]
                ),
            )

            turn = anyio.run(runtime.run_once, "read fact.txt")
            events = anyio.run(runtime.events, turn.run_id)

            self.assertEqual(turn.status, RunStatus.SUCCEEDED)
            self.assertEqual(turn.answer, "Final answer: Knuth works")
            types = [event.type for event in events]
            self.assertIn("step.started", types)
            self.assertIn("tool.batch_planned", types)
            self.assertIn("tool.proposed", types)
            self.assertIn("tool.invocation_started", types)
            self.assertIn("tool.invocation_completed", types)
            self.assertIn("tool.batch_closed", types)
            self.assertIn("run.succeeded", types)

            async def reconstruct():
                return await raw_ledger_messages_from_events(events)

            reconstructed = anyio.run(reconstruct)
            self.assertEqual(reconstructed[-1].content, "Final answer: Knuth works")

    def test_step_started_carries_context_snapshot(self) -> None:
        runtime = _build_runtime(
            ScriptedInferenceClient(
                [InferenceMessage(role=InferenceRole.ASSISTANT, content="hi")]
            ),
        )
        turn = anyio.run(runtime.run_once, "hello")
        events = anyio.run(runtime.events, turn.run_id)

        step_events = [event for event in events if event.type == "step.started"]
        self.assertEqual(len(step_events), 1)
        snapshot = step_events[0].snapshot
        self.assertEqual(snapshot.message_count, 3)
        self.assertTrue(snapshot.messages_hash)
        self.assertTrue(snapshot.model_config_hash)

    def test_approval_resume_executes_frozen_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            target = str(Path(workspace, "x.txt"))
            runtime = _build_runtime(
                ScriptedInferenceClient(
                    [
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT,
                            tool_calls=[
                                CoreToolCall(
                                    tool_call_id="call-write",
                                    name="write_file",
                                    arguments={"path": target, "content": "hello"},
                                )
                            ],
                        ),
                        InferenceMessage(role=InferenceRole.ASSISTANT, content="Done"),
                    ]
                ),
            )

            first = anyio.run(runtime.run_once, "write x")
            pending = anyio.run(runtime.pending_approvals, first.run_id)
            self.assertEqual(len(pending), 1)
            # Approval is bound to the exact frozen arguments.
            self.assertEqual(
                pending[0].args_hash,
                args_hash_for({"path": target, "content": "hello"}),
            )
            anyio.run(runtime.approve, pending[0].id)

            async def resume():
                async with runtime.resume(first.run_id) as session:
                    return await session.result()

            resumed = anyio.run(resume)

            self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
            self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
            self.assertEqual(
                Path(workspace, "x.txt").read_text(encoding="utf-8"), "hello"
            )

    def test_approval_resume_executes_tool_in_fresh_runtime(self) -> None:
        """CLI approve/resume are separate processes; execute must not rely on
        a registry index warmed by the original propose path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir, "knuth.db")
            first_runtime = build_sqlite_runtime(
                inference_client=ScriptedInferenceClient(
                    [
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT,
                            tool_calls=[
                                CoreToolCall(
                                    tool_call_id="call-shell",
                                    name="shell",
                                    arguments={"command": "printf ok"},
                                )
                            ],
                        )
                    ]
                ),
                inference_config=InferenceConfig(),
                db_path=db_path,
            )
            first = anyio.run(first_runtime.run_once, "run shell")
            pending = anyio.run(first_runtime.pending_approvals, first.run_id)
            self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
            self.assertEqual(len(pending), 1)

            approve_runtime = build_sqlite_runtime(
                inference_client=ScriptedInferenceClient(
                    [InferenceMessage(role=InferenceRole.ASSISTANT, content="unused")]
                ),
                inference_config=InferenceConfig(),
                db_path=db_path,
            )
            anyio.run(approve_runtime.approve, pending[0].id)

            resume_runtime = build_sqlite_runtime(
                inference_client=ScriptedInferenceClient(
                    [InferenceMessage(role=InferenceRole.ASSISTANT, content="done")]
                ),
                inference_config=InferenceConfig(),
                db_path=db_path,
            )

            async def resume():
                async with resume_runtime.resume(first.run_id) as session:
                    return await session.result()

            resumed = anyio.run(resume)
            events = anyio.run(resume_runtime.events, first.run_id)

        completed = [
            event for event in events if event.type == "tool.invocation_completed"
        ]
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(completed[-1].outcome, "succeeded")
        self.assertIn("ok", completed[-1].observation or "")

    def test_resume_without_resolving_approval_fails_loudly(self) -> None:
        runtime = _build_runtime(
            ScriptedInferenceClient(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        tool_calls=[
                            CoreToolCall(
                                tool_call_id="call-write",
                                name="write_file",
                                arguments={"path": "x.txt", "content": "hello"},
                            )
                        ],
                    ),
                ]
            ),
        )
        first = anyio.run(runtime.run_once, "write x")
        self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)

        async def resume():
            async with runtime.resume(first.run_id) as session:
                return await session.result()

        with self.assertRaisesRegex(LedgerError, "pending approvals"):
            anyio.run(resume)

    def test_denied_approval_resumes_and_informs_model(self) -> None:
        """A denied tool call must not deadlock the run: on resume the model
        receives a denied tool result and the run can complete."""
        client = CapturingScriptedClient(
            [
                InferenceMessage(
                    role=InferenceRole.ASSISTANT,
                    tool_calls=[
                        CoreToolCall(
                            tool_call_id="call-shell",
                            name="shell",
                            arguments={"command": "date"},
                        )
                    ],
                ),
                InferenceMessage(
                    role=InferenceRole.ASSISTANT,
                    content="Understood, I will not run that command.",
                ),
            ]
        )
        runtime = _build_runtime(client)

        first = anyio.run(runtime.run_once, "run date")
        self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
        pending = anyio.run(runtime.pending_approvals, first.run_id)
        self.assertEqual(len(pending), 1)
        anyio.run(runtime.deny, pending[0].id)

        async def resume():
            async with runtime.resume(first.run_id) as session:
                return await session.result()

        resumed = anyio.run(resume)

        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        final_turn_messages = client.captured_messages[-1]
        tool_results = [
            message
            for message in final_turn_messages
            if message.role == InferenceRole.TOOL_RESULT
        ]
        self.assertTrue(tool_results)
        self.assertIn("denied", (tool_results[-1].content or "").lower())
        self.assertEqual(anyio.run(runtime.pending_approvals, first.run_id), [])
        events = anyio.run(runtime.events, first.run_id)
        denied = [
            event
            for event in events
            if event.type == "tool.invocation_completed"
            and event.outcome == "denied"
        ]
        self.assertTrue(denied)

    def test_verification_failure_feeds_back_to_model(self) -> None:
        client = CapturingScriptedClient(
            [
                InferenceMessage(role=InferenceRole.ASSISTANT, content="   "),
                InferenceMessage(role=InferenceRole.ASSISTANT, content="Real answer"),
            ]
        )
        runtime = _build_runtime(client)
        turn = anyio.run(runtime.run_once, "answer me")

        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        self.assertEqual(turn.answer, "Real answer")
        # The second turn must see the verification feedback as a message.
        second_turn = client.captured_messages[-1]
        self.assertIn(
            EMPTY_ANSWER_FEEDBACK, [message.content for message in second_turn]
        )

    def test_large_uncondensed_observation_is_condensed_for_model_context(self) -> None:
        big = "x" * 9000
        with tempfile.TemporaryDirectory() as workspace:
            big_path = Path(workspace, "big.txt")
            big_path.write_text(big, encoding="utf-8")
            client = CapturingScriptedClient(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        tool_calls=[
                            CoreToolCall(
                                tool_call_id="call-1",
                                name="read_file",
                                arguments={"path": str(big_path)},
                            )
                        ],
                    ),
                    InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
                ]
            )
            runtime = _build_runtime(client)
            turn = anyio.run(runtime.run_once, "read big")
            events = anyio.run(runtime.events, turn.run_id)

        completed = [
            event for event in events if event.type == "tool.invocation_completed"
        ]
        self.assertIsNotNone(completed[0].observation)
        self.assertEqual(completed[0].raw_artifacts, [])
        self.assertFalse(completed[0].self_condensed)
        raw_messages = anyio.run(runtime.messages, turn.run_id)
        raw_tool_results = [
            message
            for message in raw_messages
            if message.role == InferenceRole.TOOL_RESULT
        ]
        self.assertIn(big, raw_tool_results[0].content or "")
        model_context = anyio.run(runtime.model_context_messages, turn.run_id)
        model_tool_results = [
            message
            for message in model_context
            if message.role == InferenceRole.TOOL_RESULT
        ]
        self.assertTrue(
            (model_tool_results[0].content or "").startswith(
                "Observation condensed for context headroom."
            )
        )
        audit = anyio.run(runtime.rewrite_audit, turn.run_id)
        condensation = next(
            record
            for record in audit
            if record["middleware"] == "observation_condensation"
        )
        self.assertEqual(condensation["operation"], "replace")
        self.assertEqual(len(condensation["replacement_messages"]), 1)

        # The next model request sees the projection-condensed replacement.
        final_turn = client.captured_messages[-1]
        tool_results = [
            message
            for message in final_turn
            if message.role == InferenceRole.TOOL_RESULT
        ]
        self.assertTrue(
            (tool_results[0].content or "").startswith(
                "Observation condensed for context headroom."
            )
        )
        self.assertNotIn(big, tool_results[0].content or "")

    def test_shell_l1_artifact_output_is_committed_and_not_recondensed(self) -> None:
        client = CapturingScriptedClient(
            [
                InferenceMessage(
                    role=InferenceRole.ASSISTANT,
                    tool_calls=[
                        CoreToolCall(
                            tool_call_id="call-shell",
                            name="shell",
                            arguments={"command": "printf '%05000d' 0"},
                        )
                    ],
                ),
                InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
            ]
        )
        runtime = _build_runtime(client)

        first = anyio.run(runtime.run_once, "run shell")
        pending = anyio.run(runtime.pending_approvals, first.run_id)
        self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
        self.assertEqual(len(pending), 1)
        anyio.run(runtime.approve, pending[0].id)

        async def resume():
            async with runtime.resume(first.run_id) as session:
                return await session.result()

        resumed = anyio.run(resume)
        events = anyio.run(runtime.events, first.run_id)
        completed = [
            event for event in events if event.type == "tool.invocation_completed"
        ][0]
        parsed = parse_tagged_process_output(completed.observation)
        manifest_path = (
            runtime._services.artifact_store.root / first.run_id / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        model_tool_result = [
            message
            for message in client.captured_messages[-1]
            if message.role == InferenceRole.TOOL_RESULT
        ][0]

        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertTrue(completed.self_condensed)
        self.assertEqual(len(completed.raw_artifacts), 2)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.offload["status"], "offloaded")
        self.assertEqual(parsed.offload["stdout"]["id"], completed.raw_artifacts[0])
        self.assertEqual(parsed.offload["stderr"]["id"], completed.raw_artifacts[1])
        self.assertEqual(
            Path(parsed.offload["stdout"]["path"]).read_text(encoding="utf-8"),
            "0" * 5000,
        )
        self.assertEqual(model_tool_result.content, completed.observation)
        self.assertIn("</process_output>", model_tool_result.content or "")
        self.assertEqual(
            manifest["artifacts"][completed.raw_artifacts[0]]["state"],
            "committed",
        )
        self.assertEqual(
            manifest["artifacts"][completed.raw_artifacts[1]]["state"],
            "committed",
        )
        audit = anyio.run(runtime.rewrite_audit, first.run_id)
        self.assertFalse(
            [
                record
                for record in audit
                if record["middleware"] == "observation_condensation"
            ]
        )

    def test_duplicate_message_middleware_names_are_rejected(self) -> None:
        class SameNamedMiddleware(MessageMiddleware):
            name = "same_name"
            checkpoints = {MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST}

            def __init__(self, content: str, priority: int) -> None:
                self.content = content
                self.priority = priority

            async def process(
                self,
                ctx: MessageMiddlewareContext,
                messages,
            ):
                _ = ctx, messages
                return []

        with self.assertRaisesRegex(ValueError, "duplicate message middleware name"):
            runner = MessageMiddlewareRunner(
                MemoryRunLedger(),
                [
                    SameNamedMiddleware("first", priority=1),
                    SameNamedMiddleware("second", priority=2),
                ],
            )
            self.assertIsNotNone(runner)

    def test_patch_metadata_rejects_runtime_reserved_keys(self) -> None:
        class BadMetadataMiddleware(MessageMiddleware):
            name = "bad_metadata"
            priority = 1
            checkpoints = {MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST}

            async def process(
                self,
                ctx: MessageMiddlewareContext,
                messages,
            ):
                _ = ctx, messages
                return [
                    InsertPatch(
                        position=InsertPosition(kind="before", target_id="m:2"),
                        items=[
                            InferenceMessage(
                                role=InferenceRole.SYSTEM,
                                content="bad",
                            )
                        ],
                        metadata={"rewrite_id": "spoof"},
                    )
                ]

        async def scenario():
            ledger = MemoryRunLedger()
            run = await ledger.create_run("hello")
            await ledger.apply(run.id, UserMessageDraft(content="hello"))
            runner = MessageMiddlewareRunner(ledger, [BadMetadataMiddleware()])
            await runner.run_checkpoint(
                run.id, MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST
            )

        with self.assertRaisesRegex(ValueError, "runtime-reserved keys: rewrite_id"):
            anyio.run(scenario)

    def test_model_context_messages_allows_open_tool_batch(self) -> None:
        async def scenario():
            ledger = MemoryRunLedger()
            run_id = await _setup_awaiting_approval(ledger)
            runtime = build_memory_runtime(
                inference_client=ScriptedInferenceClient([]),
                inference_config=InferenceConfig(),
                ledger=ledger,
                include_default_tools=False,
            )
            status = await runtime.status(run_id)
            messages = await runtime.model_context_messages(run_id)
            return status, messages

        status, messages = anyio.run(scenario)

        self.assertEqual(status, RunStatus.WAITING_APPROVAL)
        assistant = [m for m in messages if m.role == InferenceRole.ASSISTANT][-1]
        self.assertEqual(assistant.tool_calls[0].effective_id, "c1")

    def test_turn_closed_compaction_is_durable_and_used_on_continue(self) -> None:
        client = CapturingScriptedClient(
            [
                InferenceMessage(role=InferenceRole.ASSISTANT, content="first answer"),
                InferenceMessage(role=InferenceRole.ASSISTANT, content="second answer"),
            ]
        )
        runtime = _build_runtime(
            client,
            message_middlewares=[
                ContextCompactionMiddleware(max_messages=1, keep_last=1)
            ],
        )

        first = anyio.run(runtime.run_once, "first question")

        async def continue_once():
            async with runtime.continue_run(first.run_id, "second question") as session:
                return await session.result()

        anyio.run(continue_once)
        events = anyio.run(runtime.events, first.run_id)

        self.assertIn("message.rewrite_anchor", [event.type for event in events])
        second_call_contents = [
            message.content for message in client.captured_messages[1]
        ]
        self.assertIn(
            "Earlier context summary:\nfirst question",
            second_call_contents,
        )

    def test_compaction_can_process_later_uncompacted_messages(self) -> None:
        client = CapturingScriptedClient(
            [
                InferenceMessage(role=InferenceRole.ASSISTANT, content="first answer"),
                InferenceMessage(role=InferenceRole.ASSISTANT, content="second answer"),
            ]
        )
        runtime = _build_runtime(
            client,
            message_middlewares=[
                ContextCompactionMiddleware(max_messages=1, keep_last=1)
            ],
        )

        first = anyio.run(runtime.run_once, "first question")

        async def continue_once():
            async with runtime.continue_run(first.run_id, "second question") as session:
                return await session.result()

        anyio.run(continue_once)
        events = anyio.run(runtime.events, first.run_id)
        compaction_begins = [
            event
            for event in events
            if event.type == "message.rewrite_anchor"
            and event.kind == "begin"
            and event.middleware == "context_compaction"
        ]

        self.assertGreaterEqual(len(compaction_begins), 2)

    def test_before_model_request_compaction_recovers_missed_turn_closed_write(self) -> None:
        ledger = MemoryRunLedger()
        first_client = CapturingScriptedClient(
            [InferenceMessage(role=InferenceRole.ASSISTANT, content="first answer")]
        )
        first_runtime = build_memory_runtime(
            inference_client=first_client,
            inference_config=InferenceConfig(),
            ledger=ledger,
            message_middlewares=[],
            include_default_tools=False,
        )
        first = anyio.run(first_runtime.run_once, "first question")
        self.assertFalse(
            [
                event
                for event in anyio.run(first_runtime.events, first.run_id)
                if event.type == "message.rewrite_anchor"
                and event.middleware == "context_compaction"
            ]
        )

        second_client = CapturingScriptedClient(
            [InferenceMessage(role=InferenceRole.ASSISTANT, content="second answer")]
        )
        second_runtime = build_memory_runtime(
            inference_client=second_client,
            inference_config=InferenceConfig(),
            ledger=ledger,
            message_middlewares=[
                ContextCompactionMiddleware(max_messages=1, keep_last=1)
            ],
            include_default_tools=False,
        )

        async def continue_once():
            async with second_runtime.continue_run(
                first.run_id, "second question"
            ) as session:
                return await session.result()

        anyio.run(continue_once)
        events = anyio.run(second_runtime.events, first.run_id)

        self.assertIn("message.rewrite_anchor", [event.type for event in events])
        self.assertIn(
            "Earlier context summary:\nfirst question\nfirst answer",
            [message.content for message in second_client.captured_messages[0]],
        )


class CrashRecoveryTests(unittest.TestCase):
    def _resume(self, runtime, run_id):
        async def scenario():
            async with runtime.resume(run_id) as session:
                return await session.result()

        return anyio.run(scenario)

    def _runtime_with_ledger(self, client, ledger):
        registry = create_default_registry()
        broker = ToolBroker(registry, PolicyEngine())
        return build_memory_runtime(
            inference_client=client,
            inference_config=InferenceConfig(),
            ledger=ledger,
            tool_broker=broker,
        )

    def _simulate_crash_mid_execution(self, ledger, *, effect: ToolEffect) -> str:
        async def scenario():
            run_id = await _setup_proposed_batch(
                ledger,
                decision=ToolCallDecision.ALLOWED,
                effect=effect,
                args={"path": "fact.txt"},
                tool_name="read_file",
            )
            await ledger.apply(
                run_id,
                ToolInvocationStartedDraft(
                    tool_call_id="c1",
                    attempt=1,
                ),
            )
            # Process "crashes" here: invocation stays running, run stays RUNNING.
            return run_id

        return anyio.run(scenario)

    def test_crashed_retryable_tool_fails_and_model_recovers(self) -> None:
        ledger = MemoryRunLedger()
        run_id = self._simulate_crash_mid_execution(ledger, effect=ToolEffect.READ)
        runtime = self._runtime_with_ledger(
            ScriptedInferenceClient(
                [InferenceMessage(role=InferenceRole.ASSISTANT, content="recovered")]
            ),
            ledger,
        )
        # A leftover RUNNING run is not resumable directly: recovery is the
        # explicit gate that settles crashed work and pauses the run first.
        anyio.run(runtime.recover_crashed_runs, run_id)
        result = self._resume(runtime, run_id)
        events = anyio.run(runtime.events, run_id)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        completed = [
            event
            for event in events
            if event.type == "tool.invocation_completed" and event.outcome == "failed"
        ]
        self.assertTrue(completed)
        self.assertIn("interrupted", completed[0].observation or "")

    def test_crashed_external_write_becomes_unknown_and_needs_human(self) -> None:
        ledger = MemoryRunLedger()
        run_id = self._simulate_crash_mid_execution(
            ledger, effect=ToolEffect.EXTERNAL_WRITE
        )
        runtime = self._runtime_with_ledger(
            ScriptedInferenceClient(
                [InferenceMessage(role=InferenceRole.ASSISTANT, content="after")]
            ),
            ledger,
        )
        # Recovery first turns the leftover RUNNING run into a paused run with
        # the in-flight external write marked UNKNOWN.
        anyio.run(runtime.recover_crashed_runs, run_id)
        result = self._resume(runtime, run_id)
        self.assertEqual(result.status, RunStatus.PAUSED)

        async def invocation_status():
            return (await ledger.get_invocation("c1")).status

        self.assertEqual(
            anyio.run(invocation_status), ToolInvocationStatus.UNKNOWN
        )

        # Human resolves the unknown outcome, then the run can resume.
        resolved = anyio.run(
            runtime.resolve_unknown, "c1", "succeeded", "saw it on the server"
        )
        self.assertEqual(resolved.status, ToolInvocationStatus.SUCCEEDED)
        final = self._resume(runtime, run_id)
        self.assertEqual(final.status, RunStatus.SUCCEEDED)

    def test_recover_scan_settles_retryable_work_and_pauses_run(self) -> None:
        ledger = MemoryRunLedger()
        run_id = self._simulate_crash_mid_execution(ledger, effect=ToolEffect.READ)
        runtime = self._runtime_with_ledger(
            ScriptedInferenceClient(
                [InferenceMessage(role=InferenceRole.ASSISTANT, content="done")]
            ),
            ledger,
        )
        reports = anyio.run(runtime.recover_crashed_runs)
        self.assertEqual(
            reports, [CrashRecoveryReport(run_id=run_id, failed=1, unknown=0)]
        )
        self.assertEqual(anyio.run(runtime.status, run_id), RunStatus.PAUSED)

        async def invocation_status():
            return (await ledger.get_invocation("c1")).status

        self.assertEqual(anyio.run(invocation_status), ToolInvocationStatus.FAILED)

        # A recovered run resumes normally: the loop closes the settled
        # batch and the model sees the crash observation.
        result = self._resume(runtime, run_id)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)

    def test_recover_scan_marks_external_write_unknown(self) -> None:
        ledger = MemoryRunLedger()
        run_id = self._simulate_crash_mid_execution(
            ledger, effect=ToolEffect.EXTERNAL_WRITE
        )
        runtime = self._runtime_with_ledger(ScriptedInferenceClient([]), ledger)
        reports = anyio.run(runtime.recover_crashed_runs)
        self.assertEqual(
            reports, [CrashRecoveryReport(run_id=run_id, failed=0, unknown=1)]
        )

        async def invocation_status():
            return (await ledger.get_invocation("c1")).status

        self.assertEqual(
            anyio.run(invocation_status), ToolInvocationStatus.UNKNOWN
        )

    def test_recover_scan_skips_runs_that_are_not_running(self) -> None:
        ledger = MemoryRunLedger()

        async def scenario():
            await ledger.create_run("idle")
            runtime = build_memory_runtime(
                inference_client=ScriptedInferenceClient([]),
                inference_config=InferenceConfig(),
                ledger=ledger,
            )
            return await runtime.recover_crashed_runs()

        self.assertEqual(anyio.run(scenario), [])


class DebugSinkTests(unittest.TestCase):
    def test_sink_appends_one_jsonl_line_per_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sink = DebugEventSink(temp_dir)
            event = emit_transient_runtime_event(
                "run-1",
                ModelReasoningDeltaDraft(delta="thinking out loud"),
                event_id="evt-1",
                created_at="2026-06-11T00:00:00Z",
            )
            anyio.run(sink.handle_event, event)
            anyio.run(sink.handle_event, event)

            lines = Path(temp_dir, "run-1.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("thinking out loud", lines[0])

    def test_build_sqlite_runtime_wires_debug_sink_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = build_sqlite_runtime(
                inference_client=ScriptedInferenceClient(
                    [InferenceMessage(role=InferenceRole.ASSISTANT, content="hi")]
                ),
                inference_config=InferenceConfig(),
                db_path=Path(temp_dir, "k.db"),
                debug_sink_dir=Path(temp_dir, "debug"),
            )
            result = anyio.run(runtime.run_once, "hello")
            content = Path(
                temp_dir, "debug", f"{result.run_id}.jsonl"
            ).read_text()
        # Both transient and durable events land in the sink; transient ones
        # (reasoning, raw deltas) exist nowhere else.
        self.assertIn("run.invocation.started", content)
        self.assertIn("run.succeeded", content)


class StreamingTextClient:
    """Yields a content delta then a generation_end per scripted answer."""

    model = "streaming-model"

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls = 0

    async def stream(self, messages, tools, config, runtime=None):
        text = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        gen = f"gen-{self.calls}"
        yield InferenceContentDelta(
            generation_id=gen,
            seq=1,
            run_id=config.run_id,
            delta=text,
        )
        message = InferenceMessage(role=InferenceRole.ASSISTANT, content=text)
        yield InferenceGenerationCompleted(
            generation_id=gen,
            seq=2,
            run_id=config.run_id,
            message=message,
        )


class StreamingToolCallProjectionClient:
    model = "streaming-tool-call-model"

    async def stream(self, messages, tools, config, runtime=None):
        yield InferenceToolCallStarted(
            generation_id="gen-tool",
            seq=1,
            run_id=config.run_id,
            index=0,
            id="call-1",
        )
        yield InferenceToolCallDelta(
            generation_id="gen-tool",
            seq=2,
            run_id=config.run_id,
            index=0,
            id="call-1",
            name_delta="shell",
        )
        yield InferenceGenerationCompleted(
            generation_id="gen-tool",
            seq=3,
            run_id=config.run_id,
            message=InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
        )


class CapturingInferenceClient:
    """Records the messages handed to each ``stream`` call."""

    model = "capturing-model"

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls = 0
        self.captured_messages: list[list[InferenceMessage]] = []

    async def stream(self, messages, tools, config, runtime=None):
        self.captured_messages.append(list(messages))
        text = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        yield InferenceGenerationCompleted(
            generation_id=f"gen-{self.calls}",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(role=InferenceRole.ASSISTANT, content=text),
        )


class SystemPreambleTests(unittest.TestCase):
    def test_skill_runtime_is_builtin_without_explicit_config(self) -> None:
        client = CapturingInferenceClient(["Hello"])
        runtime = build_memory_runtime(
            inference_client=client,
            inference_config=InferenceConfig(),
            include_default_tools=False,
        )

        anyio.run(runtime.run_once, "hi")
        tools = anyio.run(runtime.tools)

        self.assertEqual(
            [tool["function"]["name"] for tool in tools],
            ["skill"],
        )
        self.assertIn("## Skill", client.captured_messages[0][0].content or "")
        reminders = [
            message
            for message in client.captured_messages[0]
            if message.role == InferenceRole.USER
            and "<system-reminder>" in (message.content or "")
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn(
            "- none: No skills available",
            reminders[0].content or "",
        )

    def test_skill_runtime_adds_system_section_and_current_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "example-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: example-skill",
                        "description: Use when an example skill is needed.",
                        "---",
                        "",
                        "Follow the example workflow.",
                    ]
                ),
                encoding="utf-8",
            )
            client = CapturingInferenceClient(["Hello"])
            runtime = build_memory_runtime(
                inference_client=client,
                inference_config=InferenceConfig(),
                include_default_tools=False,
                skill_config=SkillRuntimeConfig(
                    roots=[SkillRoot(source=SkillSource.PROJECT, path=str(root))],
                ),
            )

            anyio.run(runtime.run_once, "hi")

        first_turn_messages = client.captured_messages[0]
        self.assertEqual(first_turn_messages[0].role, InferenceRole.SYSTEM)
        self.assertIn("## Skill", first_turn_messages[0].content or "")
        reminders = [
            message
            for message in first_turn_messages
            if message.role == InferenceRole.USER
            and "<system-reminder>" in (message.content or "")
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn(
            "- example-skill: Use when an example skill is needed.",
            reminders[0].content or "",
        )

    def test_base_identity_delivered_as_leading_system_message(self) -> None:
        client = CapturingInferenceClient(["Hello"])
        runtime = _build_runtime(
            client,
            [StaticSectionProvider(SystemSectionSource.BASE, "BASE")],
        )
        anyio.run(runtime.run_once, "hi")

        first_turn_messages = client.captured_messages[0]
        self.assertEqual(first_turn_messages[0].role, InferenceRole.SYSTEM)
        self.assertTrue((first_turn_messages[0].content or "").startswith("BASE"))
        self.assertIn("## Skill", first_turn_messages[0].content or "")

    def test_sections_composed_in_provider_injection_order(self) -> None:
        client = CapturingInferenceClient(["Hello"])
        runtime = _build_runtime(
            client,
            [
                StaticSectionProvider(SystemSectionSource.BASE, "BASE"),
                StaticSectionProvider(SystemSectionSource.USER, "USER"),
            ],
        )
        anyio.run(runtime.run_once, "hi")

        first_turn_messages = client.captured_messages[0]
        self.assertEqual(first_turn_messages[0].role, InferenceRole.SYSTEM)
        self.assertTrue(
            (first_turn_messages[0].content or "").startswith("BASE\n\nUSER")
        )
        self.assertIn("## Skill", first_turn_messages[0].content or "")
        system_messages = [
            m for m in first_turn_messages if m.role == InferenceRole.SYSTEM
        ]
        self.assertEqual(len(system_messages), 1)

    def test_builtin_skill_section_exists_when_all_host_sections_empty(self) -> None:
        client = CapturingInferenceClient(["Hello"])
        runtime = _build_runtime(
            client,
            [StaticSectionProvider(SystemSectionSource.USER, None)],
        )
        anyio.run(runtime.run_once, "hi")

        first_turn_messages = client.captured_messages[0]
        self.assertEqual(first_turn_messages[0].role, InferenceRole.SYSTEM)
        self.assertIn("## Skill", first_turn_messages[0].content or "")

    def test_preamble_present_on_every_turn(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            fact_path = Path(workspace, "fact.txt")
            fact_path.write_text("ok", encoding="utf-8")
            client = CapturingScriptedClient(
                [
                    InferenceMessage(
                        role=InferenceRole.ASSISTANT,
                        content="",
                        tool_calls=[
                            CoreToolCall(
                                tool_call_id="call-1",
                                name="read_file",
                                arguments={"path": str(fact_path)},
                            )
                        ],
                    ),
                    InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
                ]
            )
            runtime = _build_runtime(
                client,
                [StaticSectionProvider(SystemSectionSource.BASE, "BASE")],
            )
            anyio.run(runtime.run_once, "read it")

        self.assertEqual(len(client.captured_messages), 2)
        for turn_messages in client.captured_messages:
            self.assertEqual(turn_messages[0].role, InferenceRole.SYSTEM)
            self.assertTrue((turn_messages[0].content or "").startswith("BASE"))
            self.assertIn("## Skill", turn_messages[0].content or "")


class AssemblePreambleTests(unittest.TestCase):
    def test_joins_sections_in_given_order(self) -> None:
        sections = [
            SystemSection(source=SystemSectionSource.USER, text="USER"),
            SystemSection(source=SystemSectionSource.BASE, text="BASE"),
        ]
        self.assertEqual(assemble_preamble(sections), "USER\n\nBASE")

    def test_skips_empty_section_text(self) -> None:
        sections = [
            SystemSection(source=SystemSectionSource.BASE, text="BASE"),
            SystemSection(source=SystemSectionSource.USER, text=""),
        ]
        self.assertEqual(assemble_preamble(sections), "BASE")

    def test_returns_none_when_no_sections(self) -> None:
        self.assertIsNone(assemble_preamble([]))


class _Collector:
    interest = RuntimeEventInterest.all()

    def __init__(self) -> None:
        self.events: list = []

    async def handle_event(self, event) -> None:
        self.events.append(event)


class _RequiredFailingListener:
    interest = RuntimeEventInterest.for_types("model.content.delta")
    required = True

    async def handle_event(self, event) -> None:
        raise RuntimeError("renderer failed")


class StreamingRuntimeTests(unittest.TestCase):
    def test_run_session_forwards_runtime_event_projection(self) -> None:
        async def scenario():
            collector = _Collector()
            runtime = _build_runtime(StreamingTextClient(["Hello there"]))
            async with runtime.start("hi", listeners=[collector]) as session:
                result = await session.result()
            return result, collector.events

        result, collected = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [event.type for event in collected]
        self.assertIn("model.content.delta", types)
        self.assertIn("model.completed", types)
        self.assertIn("run.invocation.started", types)
        self.assertIn("user.message", types)
        self.assertIn("run.invocation.ended", types)
        self.assertNotIn("inference.content.delta", types)
        self.assertTrue(all(not event.type.startswith("inference.") for event in collected))
        deltas = [event.delta for event in collected if event.type == "model.content.delta"]
        self.assertEqual(deltas, ["Hello there"])

    def test_run_session_emits_system_preamble_before_step_started(self) -> None:
        async def scenario():
            collector = _Collector()
            client = CapturingInferenceClient(["Hello"])
            runtime = _build_runtime(
                client,
                [StaticSectionProvider(SystemSectionSource.BASE, "BASE")],
            )
            async with runtime.start("hi", listeners=[collector]) as session:
                result = await session.result()
            durable_events = await runtime.events(result.run_id)
            return result, collector.events, durable_events, client.captured_messages

        result, collected, durable_events, captured_messages = anyio.run(scenario)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [event.type for event in collected]
        preamble_index = types.index("context.system_preamble.built")
        step_index = types.index("step.started")
        self.assertLess(preamble_index, step_index)
        preamble = collected[preamble_index]
        self.assertTrue((preamble.content or "").startswith("BASE"))
        self.assertIn("## Skill", preamble.content or "")
        self.assertEqual(preamble.content, captured_messages[0][0].content)
        self.assertNotIn(
            "context.system_preamble.built",
            [event.type for event in durable_events],
        )

    def test_run_session_emits_empty_system_preamble_when_none_exists(self) -> None:
        async def scenario():
            collector = _Collector()
            client = CapturingInferenceClient(["Hello"])
            ledger = MemoryRunLedger()
            registry = create_default_registry()
            broker = ToolBroker(registry, PolicyEngine())
            artifact_store = FilesystemArtifactStore(
                Path(tempfile.mkdtemp()),
                redactor=RegexSecretRedactor(),
            )
            runtime = AgentRuntime(
                RuntimeServices(
                    inference_client=client,
                    tool_broker=broker,
                    ledger=ledger,
                    artifact_store=artifact_store,
                    context_builder=ContextBuilder(ledger, broker),
                    message_middleware_runner=None,
                    skill_hot_reload_service=None,
                ),
                InferenceConfig(),
            )
            async with runtime.start("hi", listeners=[collector]) as session:
                result = await session.result()
            return result, collector.events, client.captured_messages

        result, collected, captured_messages = anyio.run(scenario)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        preamble_events = [
            event
            for event in collected
            if event.type == "context.system_preamble.built"
        ]
        self.assertEqual(len(preamble_events), 1)
        self.assertIsNone(preamble_events[0].content)
        self.assertEqual(captured_messages[0][0].role, InferenceRole.USER)

    def test_start_accepts_caller_supplied_run_id(self) -> None:
        async def scenario():
            runtime = _build_runtime(StreamingTextClient(["Hello there"]))
            async with runtime.start("hi", run_id="run_manual") as session:
                result = await session.result()
            messages = await runtime.messages("run_manual")
            return result, messages

        result, messages = anyio.run(scenario)
        self.assertEqual(result.run_id, "run_manual")
        self.assertEqual([message.content for message in messages], ["hi", "Hello there"])

    def test_required_listener_failure_raises_observation_error_with_result(self) -> None:
        async def scenario():
            runtime = _build_runtime(StreamingTextClient(["Hello there"]))
            async with runtime.start(
                "hi", listeners=[_RequiredFailingListener()]
            ) as session:
                with self.assertRaises(RuntimeObservationError) as raised:
                    await session.result()
                return raised.exception

        error = anyio.run(scenario)

        self.assertEqual(error.result.status, RunStatus.SUCCEEDED)
        self.assertEqual(error.result.answer, "Hello there")
        self.assertEqual(len(error.failures), 1)

    def test_run_session_projects_streamed_tool_call_start_without_id_collision(self) -> None:
        async def scenario():
            collector = _Collector()
            runtime = _build_runtime(StreamingToolCallProjectionClient())
            async with runtime.start("use a tool", listeners=[collector]) as session:
                result = await session.result()
            return result, collector.events

        result, collected = anyio.run(scenario)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        started = [
            event for event in collected if event.type == "model.tool_call.started"
        ]
        deltas = [event for event in collected if event.type == "model.tool_call.delta"]
        self.assertEqual(started[0].id.startswith("evt_"), True)
        self.assertEqual(started[0].tool_call_id, "call-1")
        self.assertEqual(deltas[0].tool_call_id, "call-1")

    def test_run_session_forwards_tool_lifecycle(self) -> None:
        async def scenario():
            collector = _Collector()
            with tempfile.TemporaryDirectory() as workspace:
                fact_path = Path(workspace, "fact.txt")
                fact_path.write_text("ok", encoding="utf-8")
                client = ScriptedInferenceClient(
                    [
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT,
                            content="",
                            tool_calls=[
                                CoreToolCall(
                                    tool_call_id="call-1",
                                    name="read_file",
                                    arguments={"path": str(fact_path)},
                                )
                            ],
                        ),
                        InferenceMessage(
                            role=InferenceRole.ASSISTANT, content="done"
                        ),
                    ]
                )
                runtime = _build_runtime(client)
                async with runtime.start("read it", listeners=[collector]) as session:
                    result = await session.result()
            return result, collector.events

        result, collected = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [event.type for event in collected]
        self.assertIn("tool.batch_planned", types)
        self.assertIn("tool.invocation_started", types)
        self.assertIn("tool.invocation_completed", types)
        self.assertIn("tool.batch_closed", types)

    def test_run_session_keeps_multi_turn_memory(self) -> None:
        async def scenario():
            runtime = _build_runtime(
                StreamingTextClient(["first answer", "second answer"])
            )
            collector = _Collector()
            async with runtime.start("question one", listeners=[collector]) as session:
                first = await session.result()
            async with runtime.continue_run(
                first.run_id, "question two", listeners=[collector]
            ) as session:
                second = await session.result()
            events = await runtime.events(first.run_id)
            messages = await raw_ledger_messages_from_events(events)
            return first, second, messages

        first, second, messages = anyio.run(scenario)
        self.assertEqual(first.run_id, second.run_id)
        contents = [m.content for m in messages]
        self.assertIn("question one", contents)
        self.assertIn("first answer", contents)
        self.assertIn("question two", contents)


class SqliteRuntimeFactoryTests(unittest.TestCase):
    def test_build_sqlite_runtime_round_trips_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = build_sqlite_runtime(
                inference_client=StreamingTextClient(["Hello"]),
                inference_config=InferenceConfig(timeout_s=60.0),
                db_path=Path(temp_dir, "knuth.db"),
            )
            result = anyio.run(runtime.run_once, "hi")
            status = anyio.run(runtime.status, result.run_id)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(status, RunStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
