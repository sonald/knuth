"""FastAPI app exposing a Knuth ``AgentRuntime`` over AG-UI SSE."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from knuth.core.types import RunStatus
from knuth_runtime import AgentRuntime, LedgerError
from knuth_runtime.session import RunSession
from knuth_toold import ToolProvider

from knuth_agui.client_tools import client_tool_provider_from_agui
from knuth_agui.events import (
    AGUIEvent,
    content_type,
    encode_sse,
    messages_snapshot,
    run_error,
)
from knuth_agui.listener import SSEBridgeListener
from knuth_agui.translator import AGUITranslator

_CANONICAL_RUN_ID = re.compile(r"^run_[A-Za-z0-9_-]{1,80}$")
_RESUMABLE_STATUSES = {
    RunStatus.RUNNING,
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
    tool_providers: tuple[ToolProvider, ...],
) -> Callable[[SSEBridgeListener], RunSession]:
    status = await _existing_status(runtime, thread_id)
    if status is None:
        if prompt is None:
            raise HTTPException(status_code=400, detail="no user message in request")

        def start(listener: SSEBridgeListener) -> RunSession:
            return runtime.start(
                prompt,
                run_id=thread_id,
                listeners=[listener],
                tool_providers=tool_providers,
            )

        return start

    if status == RunStatus.SUCCEEDED:
        if prompt is None:
            raise HTTPException(
                status_code=400,
                detail="a completed thread needs a new user message to continue",
            )

        def continue_run(listener: SSEBridgeListener) -> RunSession:
            return runtime.continue_run(
                thread_id,
                prompt,
                listeners=[listener],
                tool_providers=tool_providers,
            )

        return continue_run

    if status in _RESUMABLE_STATUSES:

        def resume(listener: SSEBridgeListener) -> RunSession:
            return runtime.resume(
                thread_id,
                listeners=[listener],
                tool_providers=tool_providers,
            )

        return resume

    raise HTTPException(
        status_code=409,
        detail=f"run {thread_id} is {status.value} and cannot be resumed",
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


def create_app(runtime: AgentRuntime) -> FastAPI:
    app = FastAPI(title="knuth-agui", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
        try:
            client_tools = client_tool_provider_from_agui(body.get("tools") or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tool_providers = (client_tools,) if client_tools.has_tools else ()
        make_session = await _session_factory(
            runtime,
            thread_id=thread_id,
            prompt=prompt,
            tool_providers=tool_providers,
        )

        async def event_stream() -> AsyncIterator[str]:
            listener = SSEBridgeListener()
            try:
                async with make_session(listener) as session:
                    translator = AGUITranslator(thread_id, session.run_id)
                    async for event in listener.stream:
                        for ag_event in translator.translate(event):
                            yield encode_sse(ag_event)
                        if event.type == "run.invocation.ended":
                            break
            except Exception as exc:
                yield encode_sse(
                    run_error(str(exc), code=exc.__class__.__name__)
                )
            finally:
                await listener.aclose()

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

    return app
