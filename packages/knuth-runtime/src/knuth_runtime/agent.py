from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from knuth.core.events import RuntimeEvent
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall
from knuth.core.types import RunStatus
from knuth_llmd import (
    InferenceConfig,
    InferenceClient,
    LiteLLMInferenceClient,
    load_llm_config,
    tool_spec_to_payload,
)
from knuth_runtime.approval import (
    Approval,
    ApprovalStatus,
    JsonApprovalService,
    MemoryApprovalService,
    SQLiteApprovalService,
)
from knuth_runtime.artifact_store import FileArtifactStore, MemoryArtifactStore
from knuth_runtime.context import ContextBuilder, reconstruct_messages_from_events
from knuth_runtime.hooks import HookManager
from knuth_runtime.loop import run_agent_loop
from knuth_runtime.policy import PolicyEngine
from knuth_runtime.services import RealtimeBus, RuntimeServices
from knuth_runtime.stores import EventStore, JsonStore, RunStore, SQLiteStore
from knuth_runtime.verifier import Verifier
from knuth_toold import ToolBroker, ToolExecutor, create_default_registry


@dataclass(frozen=True)
class AgentTurn:
    answer: str
    messages: tuple[InferenceMessage, ...]
    tool_calls: tuple[ToolCall, ...]
    run_id: str | None = None
    status: RunStatus | None = None


class AgentLoop:
    def __init__(
        self,
        inference_client: InferenceClient,
        inference_config: InferenceConfig,
        tool_executor: ToolExecutor,
        max_tool_rounds: int = 4,
    ) -> None:
        self._inference_client = inference_client
        self._inference_config = inference_config
        self._tool_executor = tool_executor
        self._max_tool_rounds = max_tool_rounds

    async def run_turn(
        self, user_input: str, history: tuple[InferenceMessage, ...] = ()
    ) -> AgentTurn:
        messages = list(history)
        messages.append(InferenceMessage(role=InferenceRole.USER, content=user_input))
        tool_calls: list[ToolCall] = []
        tool_specs = [tool_spec_to_payload(tool) for tool in self._tool_executor.specs()]

        for _ in range(self._max_tool_rounds + 1):
            response = await self._inference_client.complete(
                messages,
                self._inference_config,
                tools=tool_specs,
            )
            messages.append(response.message)
            if not response.message.tool_calls:
                return AgentTurn(
                    answer=response.message.content or "",
                    messages=tuple(messages),
                    tool_calls=tuple(tool_calls),
                )
            for call in response.message.tool_calls:
                tool_calls.append(call)
                result = await self._tool_executor.execute(call)
                content = result.content if result.ok else f"ERROR: {result.error}"
                messages.append(
                    InferenceMessage(
                        role=InferenceRole.TOOL_RESULT,
                        tool_name=call.name,
                        content=content or "",
                        tool_call_id=call.id,
                    )
                )

        raise RuntimeError("agent loop exceeded max_tool_rounds")


class AgentRuntime:
    def __init__(
        self,
        services: RuntimeServices | None = None,
        inference_config: InferenceConfig | None = None,
        loop: AgentLoop | None = None,
    ) -> None:
        self._services = services
        self._inference_config = inference_config
        self._loop = loop

    async def run_once(self, prompt: str) -> AgentTurn:
        if self._loop is not None:
            return await self._loop.run_turn(prompt)
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")
        run = await self._services.run_store.create(prompt)
        await self._services.event_store.append(
            run.id,
            namespace="run",
            name="created",
            payload=run.model_dump(),
        )
        await self._services.event_store.append(
            run.id,
            namespace="user",
            name="message",
            payload={"content": prompt},
        )
        status = await run_agent_loop(run.id, self._services, self._inference_config)
        events = await self._services.event_store.list_events(run.id)
        answer = _answer_from_events(events)
        return AgentTurn(
            answer=answer,
            messages=tuple(reconstruct_messages_from_events(events)),
            tool_calls=(),
            run_id=run.id,
            status=status,
        )

    async def resume(self, run_id: str) -> AgentTurn:
        if self._services is None or self._inference_config is None:
            raise RuntimeError("runtime is not configured")
        run = await self._services.run_store.get(run_id)
        if run.status in {RunStatus.WAITING_APPROVAL, RunStatus.PAUSED, RunStatus.WAITING_USER}:
            await self._services.run_store.set_status(run_id, RunStatus.RUNNING)
        status = await run_agent_loop(run_id, self._services, self._inference_config)
        events = await self._services.event_store.list_events(run_id)
        return AgentTurn(
            answer=_answer_from_events(events),
            messages=tuple(reconstruct_messages_from_events(events)),
            tool_calls=(),
            run_id=run_id,
            status=status,
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


async def build_default_runtime(db_path: Path | str | None = None) -> AgentRuntime:
    config = await load_llm_config()
    store = SQLiteStore(db_path or Path("~/.knuth/knuth.db"))
    approvals = SQLiteApprovalService(store)
    registry = create_default_registry()
    policy = PolicyEngine(approvals)
    broker = ToolBroker(registry, policy_engine=policy)
    services = RuntimeServices(
        inference_client=LiteLLMInferenceClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
        ),
        tool_broker=broker,
        run_store=store,
        event_store=store,
        artifact_store=FileArtifactStore(Path("~/.knuth/artifacts")),
        approvals=approvals,
        context_builder=ContextBuilder(store, broker),
        hooks=HookManager(),
        realtime_bus=RealtimeBus(),
        verifier=Verifier(),
    )
    return AgentRuntime(
        services=services,
        inference_config=InferenceConfig(
            model=config.model,
            timeout_s=config.timeout,
        ),
    )


def build_memory_runtime(
    inference_client,
    inference_config: InferenceConfig,
    run_store: RunStore,
    event_store: EventStore,
    approvals: MemoryApprovalService,
    tool_broker: ToolBroker,
) -> AgentRuntime:
    services = RuntimeServices(
        inference_client=inference_client,
        tool_broker=tool_broker,
        run_store=run_store,
        event_store=event_store,
        artifact_store=MemoryArtifactStore(),
        approvals=approvals,
        context_builder=ContextBuilder(event_store, tool_broker),
        hooks=HookManager(),
        realtime_bus=RealtimeBus(),
        verifier=Verifier(),
    )
    return AgentRuntime(services=services, inference_config=inference_config)


def _answer_from_events(events: list[RuntimeEvent]) -> str:
    for event in reversed(events):
        if event.namespace == "run" and event.name == "succeeded":
            return str(event.payload.get("answer") or "")
        if event.namespace == "approval" and event.name == "requested":
            approval_id = event.payload.get("id")
            return f"Waiting for approval: {approval_id}"
        if event.namespace == "user_input" and event.name == "requested":
            return str(event.payload.get("question") or "Waiting for user input")
    return ""
