"""FastAPI app exposing a Knuth ``AgentRuntime`` over AG-UI SSE."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import uuid4

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from knuth.core.commands import DEFAULT_BUILTIN_COMMAND_SPECS, build_command_catalog
from knuth.core.invocations import ToolInvocationStatus
from knuth.core.types import RunStatus
from knuth_runtime import AgentRuntime, LedgerError
from knuth_runtime.session import RunSession

from knuth_agui.client_tools import AGUIClientToolProvider
from knuth_agui.debug_routes import register_debug_routes
from knuth_agui.events import (
    AGUIEvent,
    content_type,
    encode_sse,
    messages_snapshot,
    run_error,
)
from knuth_agui.live import DuplicateActivePromptError, LiveRunManager
from knuth_agui.translator import AGUITranslator

_CANONICAL_RUN_ID = re.compile(r"^run_[A-Za-z0-9_-]{1,80}$")
# RUNNING is deliberately excluded: a live RUNNING run is attached through the
# LiveRunManager, not re-entered via resume. A RUNNING run with no live session
# in this process is not auto-recovered.
_RESUMABLE_STATUSES = {
    RunStatus.WAITING_APPROVAL,
    RunStatus.WAITING_TOOL_RESULT,
    RunStatus.PAUSED,
}


def _event_payload(event: AGUIEvent) -> dict[str, Any]:
    return event.model_dump(mode="json", by_alias=True, exclude_none=True)


def _field(body: dict[str, Any], camel: str, snake: str) -> Any:
    return body.get(camel, body.get(snake))


def _canonical_thread_id(body: dict[str, Any]) -> str:
    raw_thread_id = _field(body, "threadId", "thread_id")
    raw_run_id = _field(body, "runId", "run_id")
    if raw_thread_id and raw_run_id and raw_thread_id != raw_run_id:
        raise HTTPException(
            status_code=400,
            detail="threadId and runId must match; knuth-agui uses threadId == run_id",
        )
    raw = raw_thread_id or raw_run_id
    if raw in (None, ""):
        return f"run_{uuid4().hex}"
    if not isinstance(raw, str) or not _CANONICAL_RUN_ID.fullmatch(raw):
        raise HTTPException(
            status_code=400,
            detail="threadId must match run_[A-Za-z0-9_-]{1,80}",
        )
    return raw


def _text_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    pieces: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("content")
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces) or None


def _latest_user_prompt(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = _text_content(message.get("content"))
        if content is not None and content.strip():
            return content
    return None


async def _existing_status(
    runtime: AgentRuntime, run_id: str
) -> RunStatus | None:
    try:
        return await runtime.status(run_id)
    except KeyError:
        return None


async def _session_factory(
    runtime: AgentRuntime,
    *,
    thread_id: str,
    prompt: str | None,
) -> Callable[[Any], RunSession]:
    status = await _existing_status(runtime, thread_id)
    if status is None:
        if prompt is None:
            raise HTTPException(status_code=400, detail="no user message in request")

        def start(listener: Any) -> RunSession:
            return runtime.start(
                prompt,
                run_id=thread_id,
                listeners=[listener],
            )

        return start

    if status in {RunStatus.SUCCEEDED, RunStatus.INTERRUPTED}:
        if prompt is None:
            raise HTTPException(
                status_code=400,
                detail="a finished thread needs a new user message to continue",
            )

        def continue_run(listener: Any) -> RunSession:
            return runtime.continue_run(
                thread_id,
                prompt,
                listeners=[listener],
            )

        return continue_run

    if status in _RESUMABLE_STATUSES:
        # Resume only once the blocking control point is resolved. Otherwise the
        # ledger would reject the resume and the failure would surface only as a
        # truncated stream; return a clear 409 pointing at the right endpoint so
        # re-opening a waiting thread still shows actionable guidance.
        await _require_resumable(runtime, thread_id, status)

        def resume(listener: Any) -> RunSession:
            return runtime.resume(
                thread_id,
                listeners=[listener],
            )

        return resume

    raise HTTPException(
        status_code=409,
        detail=f"run {thread_id} is {status.value} and cannot be attached or resumed",
    )


async def _require_resumable(
    runtime: AgentRuntime, thread_id: str, status: RunStatus
) -> None:
    if status == RunStatus.WAITING_APPROVAL:
        pending = await runtime.pending_approvals(thread_id)
        if pending:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"run {thread_id} is waiting for approval; resolve it via"
                    " /approve (see /threads/{thread_id}/approvals) before"
                    " resuming"
                ),
            )
        return
    if status == RunStatus.WAITING_TOOL_RESULT:
        state = await runtime.run_state(thread_id)
        waiting = (
            state.open_batch.by_status(ToolInvocationStatus.WAITING_TOOL_RESULT)
            if state.open_batch is not None
            else ()
        )
        if waiting:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"run {thread_id} is waiting for an external tool result;"
                    " submit it via /tool_result before resuming"
                ),
            )


def _run_id_from_control_body(body: dict[str, Any]) -> str:
    run_id = _field(body, "runId", "run_id")
    if not isinstance(run_id, str) or not _CANONICAL_RUN_ID.fullmatch(run_id):
        raise HTTPException(
            status_code=400,
            detail="runId must match run_[A-Za-z0-9_-]{1,80}",
        )
    return run_id


def _tool_call_id_from_body(body: dict[str, Any]) -> str:
    tool_call_id = _field(body, "toolCallId", "tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        raise HTTPException(status_code=400, detail="toolCallId is required")
    return tool_call_id


def _tool_result_outcome(body: dict[str, Any]) -> Literal["succeeded", "failed"]:
    outcome = body.get("outcome")
    if outcome in {"succeeded", "failed"}:
        return outcome
    return "failed" if body.get("error") is not None else "succeeded"


def _tool_result_observation(body: dict[str, Any]) -> str:
    if body.get("error") is not None:
        value = body.get("error")
    elif "result" in body:
        value = body.get("result")
    elif "content" in body:
        value = body.get("content")
    else:
        value = body.get("observation", "")

    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _is_authorized(request: Request, auth_token: str) -> bool:
    authorization = request.headers.get("authorization", "")
    token = request.headers.get("x-knuth-auth-token", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    return secrets.compare_digest(token, auth_token)


def create_app(
    runtime: AgentRuntime,
    *,
    auth_token: str | None = None,
    client_tool_provider: AGUIClientToolProvider | None = None,
    interrupt_deadline_s: float = 30.0,
) -> FastAPI:
    manager = LiveRunManager(runtime, deadline_s=interrupt_deadline_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A host task group owns live sessions for the app's lifetime, so an SSE
        # disconnect only unsubscribes — the run keeps going here.
        async with anyio.create_task_group() as tg:
            manager.bind(tg)
            try:
                yield
            finally:
                await manager.shutdown()
                tg.cancel_scope.cancel()

    app = FastAPI(title="knuth-agui", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "knuth://app",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    auth_token = auth_token or None

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if (
            auth_token is not None
            and request.method != "OPTIONS"
            and request.url.path != "/healthz"
            and not _is_authorized(request, auth_token)
        ):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/commands")
    async def commands() -> dict[str, list[dict[str, Any]]]:
        skills_fn = getattr(runtime, "skills", None)
        skills = await skills_fn() if skills_fn is not None else []
        catalog = build_command_catalog(DEFAULT_BUILTIN_COMMAND_SPECS, skills)
        return {
            "commands": [
                {
                    "name": command.name,
                    "source": command.source,
                    "description": command.description,
                    "canonical": command.canonical or command.name,
                }
                for command in catalog.commands
            ]
        }

    @app.post("/agent")
    async def agent(request: Request) -> StreamingResponse:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        thread_id = _canonical_thread_id(body)
        messages = body.get("messages") or []
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="messages must be a list")
        prompt = _latest_user_prompt(messages)
        tools = body.get("tools") or []
        if tools:
            if client_tool_provider is None:
                raise HTTPException(
                    status_code=500,
                    detail="AG-UI client tool provider is not configured",
                )
            try:
                client_tool_provider.register_agui_tools(tools)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def build_factory() -> Callable[[Any], RunSession]:
            return await _session_factory(
                runtime, thread_id=thread_id, prompt=prompt
            )

        try:
            live, subscriber = await manager.start_or_attach(
                thread_id, prompt=prompt, build_factory=build_factory
            )
        except DuplicateActivePromptError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"run {thread_id} already has an active invocation; attach"
                    " without a prompt or stop it first"
                ),
            ) from exc

        async def event_stream() -> AsyncIterator[str]:
            translator = AGUITranslator(thread_id, live.run_id)
            try:
                async for event in subscriber.stream:
                    for ag_event in translator.translate(event):
                        yield encode_sse(ag_event)
                    if event.type == "run.invocation.ended":
                        break
            except Exception as exc:
                yield encode_sse(run_error(str(exc), code=exc.__class__.__name__))
            finally:
                # Passive disconnect only unsubscribes; the run keeps going.
                live.fanout.remove(subscriber)
                await subscriber.aclose()

        return StreamingResponse(event_stream(), media_type=content_type())

    @app.get("/threads")
    async def threads(limit: int = 20) -> dict[str, list[dict[str, Any]]]:
        runs = await runtime.runs(limit=limit)
        return {
            "threads": [
                {
                    "threadId": run.id,
                    "runId": run.id,
                    "status": run.status.value,
                    "query": run.query,
                    "createdAt": run.created_at,
                    "updatedAt": run.updated_at,
                    "steps": run.steps,
                    "lastSeq": run.last_seq,
                }
                for run in runs
            ]
        }

    @app.get("/threads/{thread_id}/history")
    async def history(thread_id: str) -> dict[str, Any]:
        _canonical_thread_id({"threadId": thread_id})
        try:
            messages = await runtime.messages(thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc
        return _event_payload(messages_snapshot(messages))

    @app.get("/threads/{thread_id}/messages")
    async def messages(thread_id: str) -> dict[str, Any]:
        return await history(thread_id)

    @app.get("/threads/{thread_id}/approvals")
    async def approvals(thread_id: str) -> dict[str, Any]:
        _canonical_thread_id({"threadId": thread_id})
        try:
            await runtime.status(thread_id)
            pending = await runtime.pending_approvals(thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc
        return {
            "threadId": thread_id,
            "approvals": [
                {
                    "approvalId": approval.id,
                    "runId": approval.run_id,
                    "toolCallId": approval.tool_call_id,
                    "status": approval.status.value,
                    "title": approval.title,
                    "reason": approval.reason,
                    "risk": approval.risk,
                    "preview": approval.approval_preview,
                    "createdAt": approval.created_at,
                }
                for approval in pending
            ],
        }

    @app.post("/pause")
    async def pause(request: Request) -> dict[str, str]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        run_id = _run_id_from_control_body(body)
        try:
            status = await runtime.pause(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        return {"runId": run_id, "status": status.value}

    @app.post("/stop")
    async def stop(request: Request) -> dict[str, Any]:
        """UI stop: route a graceful interrupt to the live session.

        Unlike ``/pause`` (a runtime resumable-pause control), this is the UI
        stop semantics — it interrupts active work. With no live session it is
        an idempotent no-op that reports the current durable status.
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        run_id = _run_id_from_control_body(body)
        interrupted = await manager.interrupt(run_id)
        if interrupted:
            return {"runId": run_id, "interrupted": True}
        status = await _existing_status(runtime, run_id)
        if status is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"runId": run_id, "interrupted": False, "status": status.value}

    @app.post("/tool_result")
    async def tool_result(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        run_id = _run_id_from_control_body(body)
        tool_call_id = _tool_call_id_from_body(body)
        outcome = _tool_result_outcome(body)
        observation = _tool_result_observation(body)
        try:
            invocation = await runtime.submit_tool_result(
                run_id,
                tool_call_id,
                outcome,
                observation,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LedgerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return invocation.model_dump(mode="json", by_alias=True)

    @app.post("/approve")
    async def approve(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        approval_id = _field(body, "approvalId", "approval_id")
        decision = body.get("decision")
        if not isinstance(approval_id, str) or decision not in {"approved", "denied"}:
            raise HTTPException(
                status_code=400,
                detail="approvalId and decision=approved|denied are required",
            )
        try:
            approval = (
                await runtime.approve(approval_id)
                if decision == "approved"
                else await runtime.deny(approval_id)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval not found") from exc
        return approval.model_dump(mode="json", by_alias=True)

    # Raw, untranslated event channel for the debug viewer. Kept separate from
    # the translated /agent stream: this one is full-fidelity observation only.
    register_debug_routes(
        app, runtime, manager, canonical_thread_id=_canonical_thread_id
    )

    return app
