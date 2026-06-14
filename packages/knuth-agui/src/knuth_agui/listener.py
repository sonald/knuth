"""Bridge runtime observation to an async iterator the SSE endpoint can drain.

``RunSession`` delivers ``RuntimeEvent`` values by calling a listener's
``handle_event`` from its own drain task. The HTTP handler, meanwhile, lives in
the request task and needs to *pull* events to yield them down the SSE wire.
This listener is the seam between those two tasks: ``handle_event`` pushes onto
an in-memory stream, and the endpoint iterates :pyattr:`stream`.

Backpressure: ``overflow_policy = BLOCK`` means a slow or stalled client slows
the run rather than silently dropping events — correct for a faithful event log.
"""

from __future__ import annotations

import anyio

from knuth.core.events import RuntimeEvent
from knuth_runtime.observation import RuntimeEventInterest, RuntimeEventOverflowPolicy


class SSEBridgeListener:
    interest = RuntimeEventInterest.all()
    required = False
    buffer_size = 512
    overflow_policy = RuntimeEventOverflowPolicy.BLOCK

    def __init__(self, buffer: int = 512) -> None:
        self._send, self._receive = anyio.create_memory_object_stream[RuntimeEvent](
            buffer
        )

    async def handle_event(self, event: RuntimeEvent) -> None:
        try:
            await self._send.send(event)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            # The endpoint stopped reading (client disconnected); drop quietly.
            pass

    @property
    def stream(self) -> anyio.abc.ObjectReceiveStream[RuntimeEvent]:
        return self._receive

    async def aclose(self) -> None:
        await self._send.aclose()
        await self._receive.aclose()
