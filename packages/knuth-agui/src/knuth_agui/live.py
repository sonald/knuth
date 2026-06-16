"""Host-owned live run manager for the AG-UI transport.

ADR-006/007: an SSE response is a *subscription*, not a run's lifecycle. The
``RunSession`` must outlive any single HTTP connection so a browser refresh or
dropped socket only unsubscribes — it never interrupts or pauses the run. This
manager owns live sessions in a host task group; SSE handlers attach as
subscribers and detach on disconnect. ``AgentRuntime`` keeps no session
registry; routing to the active session is this host concern.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio

from knuth.core.events import RuntimeEvent
from knuth_runtime import RunSession
from knuth_runtime.observation import RuntimeEventInterest


class DuplicateActivePromptError(Exception):
    """A new prompt arrived for a run that already has a live invocation."""


class LiveManagerNotRunningError(RuntimeError):
    """The manager was used before its host task group was bound."""


SessionFactory = Callable[["_Fanout"], RunSession]
FactoryBuilder = Callable[[], Awaitable[SessionFactory]]


class _Subscriber:
    """One SSE connection's pull stream, fed by the run's fanout."""

    def __init__(self, buffer: int = 512) -> None:
        self._send, self._receive = anyio.create_memory_object_stream[RuntimeEvent](
            buffer
        )

    async def push(self, event: RuntimeEvent) -> None:
        try:
            await self._send.send(event)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass

    @property
    def stream(self) -> anyio.abc.ObjectReceiveStream[RuntimeEvent]:
        return self._receive

    async def aclose(self) -> None:
        await self._send.aclose()
        await self._receive.aclose()

    def close_send(self) -> None:
        # End any blocked ``async for`` so the SSE handler unwinds cleanly.
        self._send.close()


class _Fanout:
    """A runtime listener that broadcasts every event to live subscribers."""

    interest = RuntimeEventInterest.all()
    required = False
    buffer_size = 512

    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()

    def add(self) -> _Subscriber:
        subscriber = _Subscriber()
        self._subscribers.add(subscriber)
        return subscriber

    def remove(self, subscriber: _Subscriber) -> None:
        self._subscribers.discard(subscriber)

    async def handle_event(self, event: RuntimeEvent) -> None:
        for subscriber in list(self._subscribers):
            await subscriber.push(event)

    def close_all(self) -> None:
        for subscriber in list(self._subscribers):
            subscriber.close_send()


@dataclass
class LiveRun:
    run_id: str
    session: RunSession
    fanout: _Fanout
    finished: anyio.Event = field(default_factory=anyio.Event)
    scope: anyio.CancelScope | None = None


class LiveRunManager:
    def __init__(self, runtime, *, deadline_s: float = 30.0) -> None:
        self._runtime = runtime
        self._deadline_s = deadline_s
        self._live: dict[str, LiveRun] = {}
        self._lock = anyio.Lock()
        self._task_group: anyio.abc.TaskGroup | None = None

    def bind(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group

    def _require_tg(self) -> anyio.abc.TaskGroup:
        if self._task_group is None:
            raise LiveManagerNotRunningError(
                "LiveRunManager used before bind(); is the app lifespan running?"
            )
        return self._task_group

    async def start_or_attach(
        self, run_id: str, *, prompt: str | None, build_factory: FactoryBuilder
    ) -> tuple[LiveRun, _Subscriber]:
        """Attach to a live run, or create one. 409 on a duplicate active prompt.

        When a live session already exists, a request without a prompt attaches
        a fresh subscriber; a request *with* a prompt is a duplicate active turn
        and is rejected rather than silently opening a second invocation.
        """
        async with self._lock:
            live = self._live.get(run_id)
            if live is not None:
                if prompt is not None:
                    raise DuplicateActivePromptError(run_id)
                return live, live.fanout.add()
            # No live session: resolve how to create one (may raise to signal a
            # 4xx for terminal/invalid durable state) before registering.
            make_session = await build_factory()
            fanout = _Fanout()
            session = make_session(fanout)
            live = LiveRun(run_id=run_id, session=session, fanout=fanout)
            self._live[run_id] = live
            subscriber = fanout.add()
            self._require_tg().start_soon(self._drive, live)
            return live, subscriber

    def attach_if_live(self, run_id: str) -> tuple[LiveRun, _Subscriber] | None:
        live = self._live.get(run_id)
        if live is None:
            return None
        return live, live.fanout.add()

    async def _drive(self, live: LiveRun) -> None:
        forced = False
        try:
            with anyio.CancelScope() as scope:
                live.scope = scope
                async with live.session as session:
                    await session.result()
        except Exception:
            # Driver-level failures are already reflected as durable run state;
            # subscribers see the error event sequence.
            pass
        finally:
            forced = live.scope is not None and live.scope.cancel_called
            async with self._lock:
                self._live.pop(live.run_id, None)
            if forced:
                # The deadline force-cancelled this invocation. We owe a
                # conservative durable outcome rather than a zombie RUNNING run:
                # recovery settles in-flight work and pauses the run.
                with anyio.CancelScope(shield=True):
                    try:
                        await self._runtime.recover_crashed_runs(live.run_id)
                    except Exception:
                        pass
            live.fanout.close_all()
            live.finished.set()

    async def interrupt(self, run_id: str, reason: str = "user_stop") -> bool:
        """Send a graceful interrupt to a live run; arm the force deadline.

        Returns whether a live session existed to interrupt. The durable
        ``INTERRUPTED`` transition is the loop's safe point, not this call.
        """
        live = self._live.get(run_id)
        if live is None:
            return False
        live.session.interrupt(reason)
        self._require_tg().start_soon(self._enforce_deadline, live)
        return True

    async def _enforce_deadline(self, live: LiveRun) -> None:
        with anyio.move_on_after(self._deadline_s):
            await live.finished.wait()
        if not live.finished.is_set() and live.scope is not None:
            # Graceful interrupt did not reach a safe point in time: force-cancel.
            # ``_drive`` then calls runtime recovery to avoid a stranded RUNNING.
            live.scope.cancel()

    def is_live(self, run_id: str) -> bool:
        return run_id in self._live

    async def shutdown(self) -> None:
        # Host shutdown: interrupt live runs so they collapse cleanly if they
        # can. Remaining work is torn down by the host task group cancellation.
        for live in list(self._live.values()):
            live.session.interrupt("shutdown")
