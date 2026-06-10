from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from knuth.core.events import (
    InferenceGenerationCompleted,
    RuntimeEvent,
)
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.runs import AgentRun
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig
from knuth_runtime.approval import (
    Approval,
    ApprovalStatus,
    MemoryApprovalService,
    SQLiteApprovalService,
)
from knuth_runtime.context import (
    ContextBuilder,
    SystemSectionProvider,
)
from knuth_runtime.observation import RuntimeEventListener
from knuth_runtime.policy import PolicyEngine
from knuth_runtime.result import RunResult
from knuth_runtime.services import RuntimeServices
from knuth_runtime.session import RunSession
from knuth_runtime.stores import EventStore, RunStore, SQLiteStore
from knuth_toold import ToolBroker, create_default_registry


class AgentRuntime:
    def __init__(
        self,
        services: RuntimeServices | None = None,
        inference_config: InferenceConfig | None = None,
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
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")
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
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")
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
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")
        return RunSession(
            mode="resume",
            services=self._services,
            inference_config=self._inference_config,
            run_id=run_id,
            listeners=listeners,
        )

    async def approve(self, approval_id: str) -> Approval:
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        return await self._services.approvals.resolve(
            approval_id, ApprovalStatus.APPROVED
        )

    async def deny(self, approval_id: str) -> Approval:
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        return await self._services.approvals.resolve(approval_id, ApprovalStatus.DENIED)

    async def status(self, run_id: str) -> RunStatus:
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        return (await self._services.run_store.get(run_id)).status

    async def pause(self, run_id: str) -> RunStatus:
        """Mark an in-flight run as paused so it can be resumed later.

        Only transitions runs that are actively progressing; waiting or
        terminal statuses are left untouched.
        """
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        run = await self._services.run_store.get(run_id)
        if run.status in {RunStatus.CREATED, RunStatus.RUNNING}:
            run = await self._services.run_store.set_status(run_id, RunStatus.PAUSED)
        return run.status

    async def runs(self, limit: int = 20) -> list[AgentRun]:
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        return await self._services.run_store.list_runs(limit)

    async def events(self, run_id: str) -> list[RuntimeEvent]:
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        return await self._services.event_store.list_events(run_id)

    async def tools(self) -> list[dict]:
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        return await self._services.tool_broker.list_visible_tools("cli")

    async def pending_approvals(self, run_id: str | None = None) -> list[Approval]:
        if self._services is None:
            raise RuntimeError("runtime is not configured")
        return await self._services.approvals.list_pending(run_id)


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
) -> AgentRuntime:
    store = SQLiteStore(db_path or Path("~/.knuth/knuth.db"))
    approvals = SQLiteApprovalService(store)
    registry = create_default_registry()
    policy = PolicyEngine(approvals)
    broker = ToolBroker(registry, policy_engine=policy)
    services = RuntimeServices(
        inference_client=inference_client,
        tool_broker=broker,
        run_store=store,
        event_store=store,
        approvals=approvals,
        context_builder=ContextBuilder(
            store,
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
    run_store: RunStore,
    event_store: EventStore,
    approvals: MemoryApprovalService,
    tool_broker: ToolBroker,
    section_providers: list[SystemSectionProvider] | None = None,
) -> AgentRuntime:
    services = RuntimeServices(
        inference_client=inference_client,
        tool_broker=tool_broker,
        run_store=run_store,
        event_store=event_store,
        approvals=approvals,
        context_builder=ContextBuilder(
            event_store,
            tool_broker,
            section_providers=section_providers,
        ),
    )
    return AgentRuntime(services=services, inference_config=inference_config)
