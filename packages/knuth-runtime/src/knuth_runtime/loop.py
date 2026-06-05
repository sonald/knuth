from __future__ import annotations

from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.events import RuntimeEvent
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig, InferenceEventType, InferenceRuntimeOptions
from knuth_runtime.context import RunContext
from knuth_runtime.hooks import HookAction, HookContext
from knuth_runtime.services import RuntimeServices
from knuth_toold import ToolIntent, ToolProposalStatus


async def run_agent_loop(
    run_id: str,
    services: RuntimeServices,
    inference_config: InferenceConfig,
    runtime_options: InferenceRuntimeOptions | None = None,
) -> RunStatus:
    turns = 0
    while True:
        run = await services.run_store.get(run_id)
        if run.status in {
            RunStatus.PAUSED,
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_USER,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run.status

        if run.status == RunStatus.CREATED:
            await services.run_store.set_status(run_id, RunStatus.RUNNING)

        pending_message = await _pending_assistant_tool_message(run_id, services)
        if pending_message is not None:
            status = await handle_tool_calls(run_id, pending_message, services)
            if status is not None:
                return status
            continue

        if turns >= run.max_turns:
            await services.event_store.append(
                run_id,
                namespace="run",
                name="failed",
                payload={"reason": "max_turns_exceeded", "max_turns": run.max_turns},
            )
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        turns += 1
        hook_result = await services.hooks.dispatch_blocking(
            HookContext(
                run_id=run_id,
                namespace="run",
                name="before_step",
                payload={"turn": turns},
            )
        )
        if hook_result.action == HookAction.PAUSE:
            await services.run_store.set_status(run_id, RunStatus.PAUSED)
            return RunStatus.PAUSED
        if hook_result.action == HookAction.TERMINATE:
            await services.run_store.set_status(run_id, RunStatus.CANCELLED)
            return RunStatus.CANCELLED

        ctx = RunContext(
            run_id=run_id,
            user_id=run.user_id,
            workspace_uri=run.metadata.get("workspace_uri"),
        )
        view = await services.context_builder.build(ctx)
        await services.event_store.append(
            run_id,
            namespace="model",
            name="started",
            payload={
                "turn": turns,
                "model": inference_config.model,
                "message_count": len(view.messages),
                "tool_count": len(view.tools),
            },
        )

        assistant_message: InferenceMessage | None = None
        stream_error: dict | None = None
        async for event in services.inference_client.stream(
            messages=view.messages,
            tools=view.tools,
            config=inference_config.model_copy(update={"run_id": run_id}),
            runtime=runtime_options,
        ):
            await services.realtime_bus.publish(run_id, event)
            if event.type == InferenceEventType.ERROR:
                stream_error = event.payload
                break
            if event.type == InferenceEventType.ABORTED:
                await services.event_store.append(
                    run_id,
                    namespace="model",
                    name="aborted",
                    payload=event.payload,
                )
                await services.run_store.set_status(run_id, RunStatus.PAUSED)
                return RunStatus.PAUSED
            if event.type == InferenceEventType.GENERATION_END:
                assistant_message = InferenceMessage.model_validate(
                    event.payload["message"]
                )

        if stream_error is not None:
            await services.event_store.append(
                run_id,
                namespace="model",
                name="failed",
                payload=stream_error,
            )
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        if assistant_message is None:
            await services.event_store.append(
                run_id,
                namespace="model",
                name="failed",
                payload={"reason": "missing_generation_end"},
            )
            await services.run_store.set_status(run_id, RunStatus.FAILED)
            return RunStatus.FAILED

        await services.event_store.append(
            run_id,
            namespace="model",
            name="completed",
            payload={"turn": turns, "assistant_message": assistant_message.model_dump()},
        )

        if assistant_message.tool_calls:
            status = await handle_tool_calls(run_id, assistant_message, services)
            if status is not None:
                return status
            continue

        verified = await services.verifier.verify_final_answer(run_id, assistant_message)
        if verified.ok:
            await services.event_store.append(
                run_id,
                namespace="run",
                name="succeeded",
                payload={"answer": assistant_message.content or "", "turns": turns},
            )
            await services.run_store.set_status(run_id, RunStatus.SUCCEEDED)
            return RunStatus.SUCCEEDED

        await services.event_store.append(
            run_id,
            namespace="verification",
            name="failed",
            payload=verified.model_dump(),
        )


async def handle_tool_calls(
    run_id: str,
    assistant_message: InferenceMessage,
    services: RuntimeServices,
) -> RunStatus | None:
    intents = [ToolIntent.from_tool_call(call) for call in assistant_message.tool_calls]
    for intent in intents:
        if intent.name == "knuth.ask_user":
            await services.event_store.append(
                run_id,
                namespace="user_input",
                name="requested",
                payload={
                    "question": intent.arguments.get("question", ""),
                    "tool_call_id": intent.id,
                },
            )
            await services.run_store.set_status(run_id, RunStatus.WAITING_USER)
            return RunStatus.WAITING_USER

    proposals = []
    for intent in intents:
        await services.event_store.append(
            run_id,
            namespace="tool",
            name="intent",
            payload=intent.model_dump(),
        )
        proposal = await services.tool_broker.propose(run_id, intent)
        await services.event_store.append(
            run_id,
            namespace="tool",
            name="proposed",
            payload=proposal.model_dump(),
        )
        if proposal.status == ToolProposalStatus.DENIED:
            error_message = InferenceMessage(
                role=InferenceRole.TOOL_RESULT,
                tool_call_id=intent.id,
                tool_name=intent.name,
                content=f"Tool call denied: {proposal.error.message if proposal.error else 'unknown'}",
            )
            await services.event_store.append(
                run_id,
                namespace="tool",
                name="completed",
                payload={
                    "intent": intent.model_dump(),
                    "message": error_message.model_dump(),
                    "denied": True,
                },
            )
            continue
        if proposal.status == ToolProposalStatus.REQUIRES_APPROVAL:
            if proposal.approval is None:
                await services.run_store.set_status(run_id, RunStatus.FAILED)
                return RunStatus.FAILED
            approval = await services.approvals.request(proposal.approval)
            await services.event_store.append(
                run_id,
                namespace="approval",
                name="requested",
                payload=approval.model_dump(),
            )
            await services.run_store.set_status(run_id, RunStatus.WAITING_APPROVAL)
            return RunStatus.WAITING_APPROVAL
        proposals.append(proposal)

    for proposal in proposals:
        await services.event_store.append(
            run_id,
            namespace="tool",
            name="started",
            payload={"intent": proposal.intent.model_dump()},
        )
        record = await services.tool_broker.execute(run_id, proposal)
        await services.event_store.append(
            run_id,
            namespace="tool",
            name="completed",
            payload={
                "intent": proposal.intent.model_dump(),
                "result": record.result.model_dump(),
                "message": record.to_tool_result_message().model_dump(),
            },
        )
    return None


async def _pending_assistant_tool_message(
    run_id: str, services: RuntimeServices
) -> InferenceMessage | None:
    events = await services.event_store.list_events(run_id)
    latest_model: tuple[int, InferenceMessage] | None = None
    completed_ids: set[str] = set()
    for event in events:
        if event.namespace == "model" and event.name == "completed":
            raw = event.payload.get("assistant_message")
            if isinstance(raw, dict):
                message = InferenceMessage.model_validate(raw)
                if message.tool_calls:
                    latest_model = (event.seq, message)
                    completed_ids.clear()
        elif (
            latest_model is not None
            and event.seq > latest_model[0]
            and event.namespace == "tool"
            and event.name == "completed"
        ):
            raw = event.payload.get("message")
            if isinstance(raw, dict):
                tool_message = InferenceMessage.model_validate(raw)
                if tool_message.tool_call_id is not None:
                    completed_ids.add(tool_message.tool_call_id)
    if latest_model is None:
        return None
    expected_ids = {call.id or f"call_{call.index}" for call in latest_model[1].tool_calls}
    if expected_ids and not expected_ids.issubset(completed_ids):
        return latest_model[1]
    return None
