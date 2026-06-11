from __future__ import annotations

from collections.abc import Iterable
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
from knuth_toold import ToolBroker, create_default_registry

from knuth_runtime.context import ContextBuilder, SystemSectionProvider
from knuth_runtime.ledger import (
    EventRedactor,
    MemoryRunLedger,
    RunLedger,
    SQLiteRunLedger,
)
from knuth_runtime.observation import RuntimeEventListener
from knuth_runtime.policy import PolicyEngine
from knuth_runtime.result import RunResult
from knuth_runtime.services import RuntimeServices
from knuth_runtime.session import RunSession


class AgentRuntime:
    """RuntimeControl: the awaited control surface for state-changing run
    operations. All transitions go through the ledger; there is no direct
    status write anywhere."""

    def __init__(
        self,
        services: RuntimeServices,
        inference_config: InferenceConfig,
    ) -> None:
        self._services = services
        self._inference_config = inference_config

    async def run_once(self, prompt: str) -> RunResult:
        async with self.start(prompt) as session:
            return await session.result()

    def start(
        self,
        prompt: str,
        *,
        listeners: Iterable[RuntimeEventListener] = (),
    ) -> RunSession:
        return RunSession(
            mode="start",
            services=self._services,
            inference_config=self._inference_config,
            prompt=prompt,
            listeners=listeners,
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
            listeners=listeners,
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
            listeners=listeners,
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
                meta={"resolved_by": "user"},
            ),
        )
        return await ledger.get_invocation(tool_call_id)

    async def status(self, run_id: str) -> RunStatus:
        return (await self._services.ledger.get_run(run_id)).status

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

    async def tools(self) -> list[dict]:
        return await self._services.tool_broker.list_visible_tools("cli")

    async def pending_approvals(self, run_id: str | None = None) -> list[Approval]:
        return await self._services.ledger.pending_approvals(run_id)


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


def build_sqlite_runtime(
    *,
    inference_client,
    inference_config: InferenceConfig,
    db_path: Path | str | None = None,
    section_providers: list[SystemSectionProvider] | None = None,
    redactor: EventRedactor | None = None,
    enable_plugins: bool = False,
) -> AgentRuntime:
    ledger = SQLiteRunLedger(db_path or Path("~/.knuth/knuth.db"), redactor=redactor)
    registry = create_default_registry(
        enable_entry_point_discovery=enable_plugins
    )
    broker = ToolBroker(registry, policy_engine=PolicyEngine())
    services = RuntimeServices(
        inference_client=inference_client,
        tool_broker=broker,
        ledger=ledger,
        context_builder=ContextBuilder(
            ledger,
            broker,
            section_providers=section_providers,
        ),
    )
    return AgentRuntime(services=services, inference_config=inference_config)


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
) -> AgentRuntime:
    ledger = ledger or MemoryRunLedger()
    if tool_broker is None:
        tool_broker = ToolBroker(
            create_default_registry(), policy_engine=PolicyEngine()
        )
    services = RuntimeServices(
        inference_client=inference_client,
        tool_broker=tool_broker,
        ledger=ledger,
        context_builder=ContextBuilder(
            ledger,
            tool_broker,
            section_providers=section_providers,
        ),
    )
    return AgentRuntime(services=services, inference_config=inference_config)
