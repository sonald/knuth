from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from knuth.core.events import (
    InferenceGenerationCompleted,
    RunCreatedDraft,
    RuntimeEvent,
    ToolCompletedDraft,
    UserMessageDraft,
)
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.tools import ToolIntent
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
from knuth_runtime.loop import run_agent_loop
from knuth_runtime.policy import PolicyEngine
from knuth_runtime.services import RuntimeServices
from knuth_runtime.stores import EventStore, RunStore, SQLiteStore
from knuth_toold import ToolBroker, create_default_registry


@dataclass(frozen=True)
class RunResult:
    answer: str
    run_id: str | None = None
    status: RunStatus | None = None


class AgentRuntime:
    def __init__(
        self,
        services: RuntimeServices | None = None,
        inference_config: InferenceConfig | None = None,
    ) -> None:
        self._services = services
        self._inference_config = inference_config

    async def run_once(self, prompt: str) -> RunResult:
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")
        run = await self._services.run_store.create(prompt)
        await self._services.event_store.append(
            run.id,
            RunCreatedDraft(query=run.query, metadata=run.metadata),
        )
        await self._services.event_store.append(
            run.id,
            UserMessageDraft(content=prompt),
        )
        status = await run_agent_loop(run.id, self._services, self._inference_config)
        events = await self._services.event_store.list_events(run.id)
        answer = _answer_from_events(events)
        return RunResult(
            answer=answer,
            run_id=run.id,
            status=status,
        )

    async def resume(self, run_id: str) -> RunResult:
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")
        run = await self._services.run_store.get(run_id)
        if run.status in {RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
            await self._services.run_store.set_status(run_id, RunStatus.RUNNING)
        status = await run_agent_loop(run_id, self._services, self._inference_config)
        events = await self._services.event_store.list_events(run_id)
        return RunResult(
            answer=_answer_from_events(events),
            run_id=run_id,
            status=status,
        )

    async def run_streaming(
        self,
        prompt: str | None,
        on_event: Callable[[RuntimeEvent], Awaitable[None]],
        *,
        run_id: str | None = None,
    ) -> RunResult:
        """Drive a run while forwarding live events to ``on_event``.

        - ``run_id is None``: start a fresh run from ``prompt``.
        - ``run_id`` set with ``prompt``: continue the same run with a new user
          turn (multi-turn memory), or answer a pending ``ask_user`` request.
        - ``run_id`` set with ``prompt is None``: resume a paused/awaiting run
          (e.g. after an approval is resolved).
        """
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")

        if run_id is None:
            if prompt is None:
                raise ValueError("prompt is required to start a new run")
            run = await self._services.run_store.create(prompt)
            await self._services.event_store.append(
                run.id,
                RunCreatedDraft(query=run.query, metadata=run.metadata),
            )
            await self._services.event_store.append(
                run.id,
                UserMessageDraft(content=prompt),
            )
            run_id = run.id
        elif prompt is not None:
            run = await self._services.run_store.get(run_id)
            if run.status == RunStatus.WAITING_USER:
                await self._record_user_answer(run_id, prompt)
            else:
                await self._services.event_store.append(
                    run_id,
                    UserMessageDraft(content=prompt),
                )
            await self._services.run_store.set_status(run_id, RunStatus.RUNNING)
        else:
            run = await self._services.run_store.get(run_id)
            if run.status in {RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
                await self._services.run_store.set_status(run_id, RunStatus.RUNNING)

        status = await run_agent_loop(
            run_id,
            self._services,
            self._inference_config,
            on_event=on_event,
        )
        events = await self._services.event_store.list_events(run_id)
        return RunResult(
            answer=_answer_from_events(events),
            run_id=run_id,
            status=status,
        )

    async def _record_user_answer(self, run_id: str, answer: str) -> None:
        events = await self._services.event_store.list_events(run_id)
        tool_call_id: str | None = None
        for event in reversed(events):
            if event.type == "user_input.requested":
                tool_call_id = event.tool_call_id
                break
        message = InferenceMessage(
            role=InferenceRole.TOOL_RESULT,
            tool_call_id=tool_call_id,
            tool_name="knuth.ask_user",
            content=answer,
        )
        await self._services.event_store.append(
            run_id,
            ToolCompletedDraft(
                intent=ToolIntent(
                    id=tool_call_id or "call_ask_user",
                    name="knuth.ask_user",
                ),
                message=message,
                outcome="answered",
            ),
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


def _answer_from_events(events: list[RuntimeEvent]) -> str:
    for event in reversed(events):
        if event.type == "run.succeeded":
            return event.answer
        if event.type == "approval.requested":
            return f"Waiting for approval: {event.approval_id}"
        if event.type == "user_input.requested":
            return event.question or "Waiting for user input"
    return ""
