"""Raw ``RuntimeEvent`` debug channel for the AG-UI transport.

The ``/agent`` SSE endpoint runs every event through :class:`AGUITranslator`,
which is deliberately lossy: it surfaces only the handful of event types a chat
UI needs to render and drops the rest (step snapshots, tool proposals, approval
resolution, message rewrites, pause/interrupt facts, verification failures, …).

Debugging the runtime needs the opposite: full-fidelity, untranslated events.
These routes expose the ledger's stored ``RuntimeEvent`` values directly and a
live tail that mirrors the raw fanout, so a debug viewer can watch exactly what
the runtime emitted. They are pure observation — they never create, advance,
interrupt, or pause a run; a dropped connection only unsubscribes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from knuth_agui.live import LiveRunManager


def _sse(obj: dict[str, Any]) -> str:
    """Encode one debug frame as a Server-Sent Events ``data:`` line.

    These are raw Knuth frames, not AG-UI protocol events, so they bypass the
    AG-UI encoder. ``default=str`` keeps the stream resilient to any field a new
    event type adds before the viewer knows about it.
    """

    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


def _event_json(event: Any) -> dict[str, Any]:
    return event.model_dump(mode="json", by_alias=True)


def register_debug_routes(
    app: FastAPI,
    runtime: Any,
    manager: LiveRunManager,
    *,
    canonical_thread_id,
) -> None:
    """Mount the raw-event debug endpoints on an existing AG-UI app.

    ``canonical_thread_id`` is the same validator ``create_app`` uses for the
    chat endpoints, passed in so debug ids are validated identically.
    """

    async def _ensure_run_exists(thread_id: str) -> None:
        try:
            await runtime.status(thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc

    @app.get("/threads/{thread_id}/events")
    async def debug_events(
        thread_id: str, after_seq: int | None = None
    ) -> dict[str, Any]:
        """Replay durable, full-fidelity ``RuntimeEvent`` values for a run.

        ``after_seq`` returns only events newer than a seq the caller already
        has, so a viewer with no live session can poll forward cheaply.
        """

        canonical_thread_id({"threadId": thread_id})
        await _ensure_run_exists(thread_id)
        events = await runtime.events(thread_id)
        if after_seq is not None:
            events = [event for event in events if event.seq > after_seq]
        payload = [_event_json(event) for event in events]
        last_seq = events[-1].seq if events else after_seq
        return {"runId": thread_id, "events": payload, "lastSeq": last_seq}

    @app.get("/threads/{thread_id}/events/stream")
    async def debug_events_stream(
        thread_id: str, request: Request
    ) -> StreamingResponse:
        """Raw event SSE: replay durable history, then tail live events.

        The frames are envelopes — ``{"phase": "replay"|"live", "event": {...}}``
        for events and ``{"phase": "control", "control": ...}`` for sentinels —
        so the viewer can tell history from real time and know when the initial
        replay is complete. Attaching to the live fanout happens *before* the
        replay cut is computed so no event slips through the seam; live durable
        events already covered by the replay are dropped by seq.
        """

        canonical_thread_id({"threadId": thread_id})
        await _ensure_run_exists(thread_id)

        # Attach first, then snapshot history: any durable event that lands
        # between attach and snapshot appears in both the replay and the live
        # feed, and is de-duplicated below by ``seq <= cut``.
        attached = manager.attach_if_live(thread_id)
        events = await runtime.events(thread_id)
        cut = events[-1].seq if events else 0

        async def event_stream() -> AsyncIterator[str]:
            try:
                for event in events:
                    yield _sse({"phase": "replay", "event": _event_json(event)})
                yield _sse(
                    {
                        "phase": "control",
                        "control": "replay_complete",
                        "lastSeq": cut,
                        "live": attached is not None,
                    }
                )
                if attached is None:
                    return
                live, subscriber = attached
                try:
                    async for event in subscriber.stream:
                        # Durable events with a replayed seq are duplicates;
                        # transient events have no seq and always pass through.
                        seq = getattr(event, "seq", None)
                        if seq is not None and seq <= cut:
                            continue
                        yield _sse({"phase": "live", "event": _event_json(event)})
                finally:
                    live.fanout.remove(subscriber)
                    await subscriber.aclose()
            except Exception as exc:  # pragma: no cover - defensive stream guard
                yield _sse(
                    {
                        "phase": "control",
                        "control": "error",
                        "message": str(exc),
                        "code": exc.__class__.__name__,
                    }
                )

        return StreamingResponse(event_stream(), media_type="text/event-stream")
