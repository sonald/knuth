from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from knuth.core.events import (
    InferenceGenerationCompleted,
    RuntimeEvent,
)
from knuth.core.invocations import Approval, ToolInvocation
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.runs import AgentRun
from knuth.core.runtime_events import (
    ApprovalResolvedDraft,
    RunPausedDraft,
    ToolInvocationCompletedDraft,
)
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig
from knuth_toold import ToolBroker, ToolProvider, ToolRegistry, create_default_registry
from knuth_toold.skills import SkillHotReloadService, SkillManager, SkillToolProvider

from knuth_runtime.context import (
    ContextBuilder,
    ContextRedactor,
    SystemSectionProvider,
    project_messages_from_events,
    raw_ledger_messages_from_events,
)
from knuth_runtime.ledger import (
    EventRedactor,
    MemoryRunLedger,
    RefoldStats,
    RunLedger,
    SQLiteRunLedger,
)
from knuth_runtime.middleware import (
    ContextCompactionMiddleware,
    MessageMiddleware,
    MessageMiddlewareCheckpoint,
    MessageMiddlewareRunner,
    ToolResultRedactionMiddleware,
)
from knuth_runtime.skills import (
    SkillChangeNoticeMiddleware,
    SkillNoticeState,
    SkillReminderMiddleware,
    SkillRuntimeConfig,
    SkillSystemSectionProvider,
)
from knuth_runtime.debug import DebugEventSink
from knuth_runtime.loop import settle_crashed_invocations
from knuth_runtime.observation import RuntimeEventListener
from knuth_runtime.policy import PolicyEngine
from knuth_runtime.redaction import RegexSecretRedactor
from knuth_runtime.result import RunResult
from knuth_runtime.services import RuntimeServices
from knuth_runtime.session import RunSession


@dataclass(frozen=True)
class CrashRecoveryReport:
    """Outcome of recovering one crashed run: in-flight invocations settled
    by effect split, then the run paused so its durable status stops lying."""

    run_id: str
    failed: int
    unknown: int


class AgentRuntime:
    """RuntimeControl: the awaited control surface for state-changing run
    operations. All transitions go through the ledger; there is no direct
    status write anywhere."""

    def __init__(
        self,
        services: RuntimeServices,
        inference_config: InferenceConfig,
        default_listeners: Iterable[RuntimeEventListener] = (),
    ) -> None:
        self._services = services
        self._inference_config = inference_config
        self._default_listeners = tuple(default_listeners)

    async def run_once(self, prompt: str) -> RunResult:
        async with self.start(prompt) as session:
            return await session.result()

    def start(
        self,
        prompt: str,
        *,
        run_id: str | None = None,
        listeners: Iterable[RuntimeEventListener] = (),
    ) -> RunSession:
        return RunSession(
            mode="start",
            services=self._services,
            inference_config=self._inference_config,
            prompt=prompt,
            run_id=run_id,
            listeners=(*self._default_listeners, *listeners),
        )

    def continue_run(
        self,
        run_id: str,
        prompt: str,
        *,
        listeners: Iterable[RuntimeEventListener] = (),
    ) -> RunSession:
        return RunSession(
            mode="continue",
            services=self._services,
            inference_config=self._inference_config,
            run_id=run_id,
            prompt=prompt,
            listeners=(*self._default_listeners, *listeners),
        )

    def resume(
        self,
        run_id: str,
        *,
        listeners: Iterable[RuntimeEventListener] = (),
    ) -> RunSession:
        return RunSession(
            mode="resume",
            services=self._services,
            inference_config=self._inference_config,
            run_id=run_id,
            listeners=(*self._default_listeners, *listeners),
        )

    async def approve(self, approval_id: str) -> Approval:
        return await self._resolve_approval(approval_id, "approved")

    async def deny(self, approval_id: str) -> Approval:
        return await self._resolve_approval(approval_id, "denied")

    async def _resolve_approval(
        self, approval_id: str, resolution: Literal["approved", "denied"]
    ) -> Approval:
        ledger = self._services.ledger
        approval = await ledger.get_approval(approval_id)
        await ledger.apply(
            approval.run_id,
            ApprovalResolvedDraft(approval_id=approval_id, resolution=resolution),
        )
        return await ledger.get_approval(approval_id)

    async def resolve_unknown(
        self,
        tool_call_id: str,
        outcome: Literal["succeeded", "failed"],
        note: str | None = None,
    ) -> ToolInvocation:
        """Human resolution for an UNKNOWN external-write outcome.

        Appends the human-confirmed completion; the batch can close afterwards.
        """
        ledger = self._services.ledger
        invocation = await ledger.get_invocation(tool_call_id)
        observation = (
            f"Outcome confirmed by user: the tool call {outcome}."
            + (f" Note: {note}" if note else "")
        )
        await ledger.apply(
            invocation.run_id,
            ToolInvocationCompletedDraft(
                tool_call_id=tool_call_id,
                tool_name=invocation.tool_name,
                outcome=outcome,
                observation=observation,
                tool_status="resolved_by_user",
            ),
        )
        await self._run_after_tool_result_committed(invocation.run_id)
        return await ledger.get_invocation(tool_call_id)

    async def submit_tool_result(
        self,
        run_id: str,
        tool_call_id: str,
        outcome: Literal["succeeded", "failed"],
        observation: str,
        *,
        tool_status: str = "client_tool_result",
    ) -> ToolInvocation:
        """Record a result supplied by an external/client tool executor.

        This only appends the observation. The caller must resume the run
        explicitly so transport adapters do not hide control-flow ownership.
        """
        ledger = self._services.ledger
        invocation = await ledger.get_invocation(tool_call_id)
        if invocation.run_id != run_id:
            raise KeyError(
                f"tool invocation {tool_call_id} does not belong to run {run_id}"
            )
        await ledger.apply(
            run_id,
            ToolInvocationCompletedDraft(
                tool_call_id=tool_call_id,
                tool_name=invocation.tool_name,
                outcome=outcome,
                observation=observation,
                tool_status=tool_status,
            ),
        )
        await self._run_after_tool_result_committed(run_id)
        return await ledger.get_invocation(tool_call_id)

    async def _run_after_tool_result_committed(self, run_id: str) -> None:
        runner = self._services.message_middleware_runner
        if runner is None:
            return
        await runner.run_checkpoint(
            run_id,
            MessageMiddlewareCheckpoint.AFTER_TOOL_RESULT_COMMITTED,
        )

    async def status(self, run_id: str) -> RunStatus:
        return (await self._services.ledger.get_run(run_id)).status

    async def run_state(self, run_id: str):
        """Current run projection: status, open batch, and pending approvals."""
        return await self._services.ledger.run_state(run_id)

    async def pause(self, run_id: str) -> RunStatus:
        """Mark an in-flight run as paused so it can be resumed later.

        Only transitions runs that are actively progressing; waiting or
        terminal statuses are left untouched.
        """
        ledger = self._services.ledger
        run = await ledger.get_run(run_id)
        if run.status in {RunStatus.CREATED, RunStatus.RUNNING}:
            await ledger.apply(run_id, RunPausedDraft(reason="paused by user"))
            run = await ledger.get_run(run_id)
        return run.status

    async def runs(self, limit: int = 20) -> list[AgentRun]:
        return await self._services.ledger.list_runs(limit)

    async def events(self, run_id: str) -> list[RuntimeEvent]:
        return await self._services.ledger.list_events(run_id)

    async def messages(self, run_id: str) -> list[InferenceMessage]:
        events = await self._services.ledger.list_events(run_id)
        return await raw_ledger_messages_from_events(
            events, self._services.ledger.get_artifact_text
        )

    async def model_context_messages(self, run_id: str) -> list[InferenceMessage]:
        events = await self._services.ledger.list_events(run_id)
        state = await self._services.ledger.run_state(run_id)
        return await project_messages_from_events(
            events,
            self._services.ledger.get_artifact_text,
            allow_open_tool_batch=state.open_batch is not None,
        )

    async def rewrite_audit(self, run_id: str) -> list[dict]:
        events = await self._services.ledger.list_events(run_id)
        audit: list[dict] = []
        active: dict[str, dict] = {}
        for event in events:
            if event.type == "message.rewrite_anchor" and event.kind == "begin":
                record = {
                    "rewrite_id": event.rewrite_id,
                    "middleware": event.middleware,
                    "operation": event.operation,
                    "position": event.position.model_dump()
                    if event.position is not None
                    else None,
                    "suppresses": list(event.suppresses),
                    "metadata": dict(event.metadata),
                    "replacement_messages": [],
                    "begin_seq": event.seq,
                    "end_seq": None,
                }
                active[event.rewrite_id] = record
                audit.append(record)
            elif event.type == "message.rewrite_message":
                record = active.get(event.rewrite_id)
                if record is not None:
                    record["replacement_messages"].append(
                        {
                            "message_id": event.message_id,
                            "role": event.message.role.value,
                            "content": event.message.content,
                            "tool_call_id": event.message.tool_call_id,
                            "tool_name": event.message.tool_name,
                            "metadata": dict(event.metadata),
                            "seq": event.seq,
                        }
                    )
            elif event.type == "message.rewrite_anchor" and event.kind == "end":
                record = active.pop(event.rewrite_id, None)
                if record is not None:
                    record["end_seq"] = event.seq
        return audit

    async def tools(self) -> list[dict]:
        return await self._services.tool_broker.list_visible_tools("cli")

    async def pending_approvals(self, run_id: str | None = None) -> list[Approval]:
        return await self._services.ledger.pending_approvals(run_id)

    async def refold(self) -> RefoldStats:
        """Drop the derived projections and rebuild them from the event log.

        Projection schema changes are not data migrations (design rule three);
        this is the rebuild path that makes that rule real.
        """
        return await self._services.ledger.refold()

    async def recover_crashed_runs(
        self, run_id: str | None = None, *, abandon_unstarted: bool = False
    ) -> list[CrashRecoveryReport]:
        """Settle work a dead or force-stopped process left in flight.

        For every RUNNING run (or just ``run_id``): running invocations are
        settled by effect split — retryable effects fail with a crash
        observation, external writes become UNKNOWN — and the run is paused,
        so durable state no longer claims an execution that is not happening.

        When ``abandon_unstarted`` is set (the host/live-manager force-cancel
        path, where the cancellation followed a *user stop*), the open batch's
        unstarted invocations also receive abandoned observations, so a later
        resume cannot run tools from a turn the user already stopped. Plain
        crash recovery leaves them as the batch's resume point.

        v0 has no worker lease, so liveness cannot be proven from the ledger;
        this runs only on explicit request (``knuth recover``) or the host's own
        force-cancel cleanup, never automatically against runs another process
        may still be driving.
        """
        ledger = self._services.ledger
        if run_id is not None:
            candidates = [await ledger.get_run(run_id)]
        else:
            candidates = await ledger.list_runs(limit=1000, status=RunStatus.RUNNING)

        reports: list[CrashRecoveryReport] = []
        for run in candidates:
            if run.status != RunStatus.RUNNING:
                continue
            state = await ledger.run_state(run.id)

            async def apply_event(draft, _run_id: str = run.id) -> None:
                await ledger.apply(_run_id, draft)

            failed = unknown = 0
            if state.open_batch is not None:
                failed, unknown = await settle_crashed_invocations(
                    apply_event,
                    state.open_batch,
                    lambda _run_id=run.id: self._run_after_tool_result_committed(
                        _run_id
                    ),
                )
                if abandon_unstarted:
                    await self._abandon_unstarted_in_batch(
                        apply_event,
                        state.open_batch,
                        lambda _run_id=run.id: self._run_after_tool_result_committed(
                            _run_id
                        ),
                    )
            await ledger.apply(
                run.id,
                RunPausedDraft(reason="recovered after crash; resume to continue"),
            )
            reports.append(
                CrashRecoveryReport(run_id=run.id, failed=failed, unknown=unknown)
            )
        return reports

    @staticmethod
    async def _abandon_unstarted_in_batch(
        apply_event, batch, after_completion=None
    ) -> None:
        from knuth.core.invocations import ToolInvocationStatus

        unstarted = batch.by_status(
            ToolInvocationStatus.PROPOSED,
            ToolInvocationStatus.AWAITING_APPROVAL,
            ToolInvocationStatus.APPROVED,
        )
        for inv in unstarted:
            if inv.observation_recorded:
                continue
            await apply_event(
                ToolInvocationCompletedDraft(
                    tool_call_id=inv.tool_call_id,
                    tool_name=inv.tool_name,
                    outcome="interrupted",
                    observation=(
                        f"Tool {inv.tool_name} was not executed: the user stopped"
                        " this turn before it ran."
                    ),
                    tool_status="abandoned",
                )
            )
            if after_completion is not None:
                await after_completion()


class _DemoInferenceClient:
    model = "knuth-demo"

    async def stream(self, messages, tools, config, runtime=None):
        yield InferenceGenerationCompleted(
            generation_id="demo-generation",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT,
                content="Knuth demo runtime is configured.",
            ),
        )


def _build_tool_registry(
    *,
    include_default_tools: bool,
    enable_plugins: bool,
) -> ToolRegistry:
    if include_default_tools:
        return create_default_registry(enable_entry_point_discovery=enable_plugins)
    return ToolRegistry(enable_entry_point_discovery=enable_plugins)


def _default_message_middlewares() -> list[MessageMiddleware]:
    return [
        ToolResultRedactionMiddleware(),
        ContextCompactionMiddleware(),
    ]


def _skill_manager(skill_config: SkillRuntimeConfig | None) -> SkillManager:
    config = skill_config or SkillRuntimeConfig()
    manager = SkillManager(config.roots)
    manager.refresh_if_dirty()
    return manager


def _compose_section_providers(
    section_providers: list[SystemSectionProvider] | None,
    skill_manager: SkillManager | None,
) -> list[SystemSectionProvider] | None:
    sections = list(section_providers or [])
    if skill_manager is not None:
        sections.append(SkillSystemSectionProvider())
    return sections or None


def _compose_message_middlewares(
    message_middlewares: list[MessageMiddleware] | None,
    skill_manager: SkillManager | None,
) -> list[MessageMiddleware]:
    middlewares = (
        list(message_middlewares)
        if message_middlewares is not None
        else _default_message_middlewares()
    )
    if skill_manager is not None:
        notice_state = SkillNoticeState()
        middlewares.extend(
            [
                SkillReminderMiddleware(skill_manager, notice_state),
                SkillChangeNoticeMiddleware(skill_manager, notice_state),
            ]
        )
    return middlewares


def _skill_hot_reload_service(
    skill_config: SkillRuntimeConfig | None,
    skill_manager: SkillManager,
) -> SkillHotReloadService | None:
    config = skill_config or SkillRuntimeConfig()
    if not config.hot_reload:
        return None
    return SkillHotReloadService(
        skill_manager,
        debounce_ms=config.hot_reload_debounce_ms,
    )


def build_sqlite_runtime(
    *,
    inference_client,
    inference_config: InferenceConfig,
    db_path: Path | str | None = None,
    section_providers: list[SystemSectionProvider] | None = None,
    message_middlewares: list[MessageMiddleware] | None = None,
    tool_providers: Iterable[ToolProvider] = (),
    include_default_tools: bool = True,
    redactor: EventRedactor | None = None,
    enable_plugins: bool = False,
    debug_sink_dir: Path | str | None = None,
    skill_config: SkillRuntimeConfig | None = None,
) -> AgentRuntime:
    # Redaction is a v0 security floor (design §8): the ledger is append-only,
    # so secrets must be stripped before apply. Default on; pass a custom
    # redactor to change what counts as a secret.
    redactor = redactor or RegexSecretRedactor()
    ledger = SQLiteRunLedger(db_path or Path("~/.knuth/knuth.db"), redactor=redactor)
    registry = _build_tool_registry(
        include_default_tools=include_default_tools,
        enable_plugins=enable_plugins,
    )
    skill_manager = _skill_manager(skill_config)
    skill_hot_reload_service = _skill_hot_reload_service(skill_config, skill_manager)
    registry.add_provider(SkillToolProvider(skill_manager))
    for provider in tool_providers:
        registry.add_provider(provider)
    broker = ToolBroker(registry, policy_engine=PolicyEngine())
    message_middleware_runner = MessageMiddlewareRunner(
        ledger,
        _compose_message_middlewares(message_middlewares, skill_manager),
    )
    services = RuntimeServices(
        inference_client=inference_client,
        tool_broker=broker,
        ledger=ledger,
        message_middleware_runner=message_middleware_runner,
        skill_hot_reload_service=skill_hot_reload_service,
        context_builder=ContextBuilder(
            ledger,
            broker,
            section_providers=_compose_section_providers(
                section_providers, skill_manager
            ),
            redactor=redactor if isinstance(redactor, ContextRedactor) else None,
        ),
    )
    default_listeners: tuple[RuntimeEventListener, ...] = ()
    if debug_sink_dir is not None:
        default_listeners = (DebugEventSink(debug_sink_dir),)
    return AgentRuntime(
        services=services,
        inference_config=inference_config,
        default_listeners=default_listeners,
    )


async def build_default_runtime(db_path: Path | str | None = None) -> AgentRuntime:
    """Build a demo/test runtime without agent-specific configuration policy."""
    return build_sqlite_runtime(
        inference_client=_DemoInferenceClient(),
        inference_config=InferenceConfig(),
        db_path=db_path,
    )


def build_memory_runtime(
    inference_client,
    inference_config: InferenceConfig,
    ledger: RunLedger | None = None,
    tool_broker: ToolBroker | None = None,
    section_providers: list[SystemSectionProvider] | None = None,
    message_middlewares: list[MessageMiddleware] | None = None,
    tool_providers: Iterable[ToolProvider] = (),
    include_default_tools: bool = True,
    skill_config: SkillRuntimeConfig | None = None,
) -> AgentRuntime:
    ledger = ledger or MemoryRunLedger()
    skill_manager = _skill_manager(skill_config)
    skill_hot_reload_service = _skill_hot_reload_service(skill_config, skill_manager)
    if tool_broker is None:
        registry = _build_tool_registry(
            include_default_tools=include_default_tools,
            enable_plugins=False,
        )
        registry.add_provider(SkillToolProvider(skill_manager))
        for provider in tool_providers:
            registry.add_provider(provider)
        tool_broker = ToolBroker(
            registry, policy_engine=PolicyEngine()
        )
    else:
        tool_broker.registry.add_provider(SkillToolProvider(skill_manager))
    message_middleware_runner = MessageMiddlewareRunner(
        ledger,
        _compose_message_middlewares(message_middlewares, skill_manager),
    )
    services = RuntimeServices(
        inference_client=inference_client,
        tool_broker=tool_broker,
        ledger=ledger,
        message_middleware_runner=message_middleware_runner,
        skill_hot_reload_service=skill_hot_reload_service,
        context_builder=ContextBuilder(
            ledger,
            tool_broker,
            section_providers=_compose_section_providers(
                section_providers, skill_manager
            ),
        ),
    )
    return AgentRuntime(services=services, inference_config=inference_config)
