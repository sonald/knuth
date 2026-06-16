from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import anyio

from knuth.core.interrupts import InterruptSignal

_DEFAULT_REASON = "user_stop"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class InterruptController:
    """Owns one invocation's sticky :class:`InterruptSignal`.

    The first ``interrupt(reason)`` wins; later calls never overwrite the reason.
    A child controller inherits a parent interrupt, but a child interrupt does
    not propagate back up. Cleanup callbacks may release local resources but must
    not write the ledger — durable facts belong only at runtime safe points.
    """

    def __init__(self, *, run_id: str | None = None) -> None:
        self._run_id = run_id
        self._interrupted = False
        self._reason: str | None = None
        self._created_at: str | None = None
        self._event = anyio.Event()
        self._wakeups: dict[object, Callable[[], None]] = {}
        self._cleanups: list[Callable[[], None]] = []
        self._children: list[InterruptController] = []

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def created_at(self) -> str | None:
        return self._created_at

    def interrupt(self, reason: str = _DEFAULT_REASON) -> bool:
        """Trigger the sticky signal. Returns whether this call first flipped it.

        A ``True`` return only means the signal went from untriggered to
        triggered; it does not promise active work was running, nor that durable
        state has reached ``INTERRUPTED`` — that is the loop's safe point.
        """
        if self._interrupted:
            return False
        self._interrupted = True
        self._reason = reason
        self._created_at = _utc_now()
        self._event.set()
        # Wake any blocking awaits, run local cleanups, and propagate downward.
        for wakeup in list(self._wakeups.values()):
            wakeup()
        for cleanup in list(self._cleanups):
            cleanup()
        for child in list(self._children):
            child.interrupt(reason)
        return True

    async def checkpoint(self) -> None:
        # Yield control so a poll-friendly loop can observe ``interrupted`` after
        # returning; never raises, so it cannot be confused with cancellation.
        await anyio.lowlevel.checkpoint()

    async def wait_interrupted(self) -> None:
        await self._event.wait()

    def add_cleanup(self, callback: Callable[[], None]) -> None:
        if self._interrupted:
            callback()
            return
        self._cleanups.append(callback)

    def register_wakeup(self, callback: Callable[[], None]) -> object:
        """Register a wakeup for a single blocking await (e.g. cancel a scope).

        Called immediately if already interrupted, so a registration that races
        the interrupt still wakes. Returns a token for :meth:`unregister_wakeup`.
        """
        token = object()
        self._wakeups[token] = callback
        if self._interrupted:
            callback()
        return token

    def unregister_wakeup(self, token: object) -> None:
        self._wakeups.pop(token, None)

    @contextlib.contextmanager
    def wakeup_scope(self, callback: Callable[[], None]) -> Iterator[None]:
        token = self.register_wakeup(callback)
        try:
            yield
        finally:
            self.unregister_wakeup(token)

    def child(self) -> InterruptController:
        child = InterruptController(run_id=self._run_id)
        self._children.append(child)
        if self._interrupted and self._reason is not None:
            child.interrupt(self._reason)
        return child

    @property
    def signal(self) -> InterruptSignal:
        # The controller itself satisfies the read/observe surface of the
        # protocol; consumers receive it typed as InterruptSignal so they cannot
        # trigger or rewire it.
        return self


def shielded_ledger_writes() -> anyio.CancelScope:
    """A shield for durable writes on the cancellation unwind path.

    When backing cancellation woke a blocking await, the safe point still has to
    land its durable facts; running those appends inside this shield keeps the
    in-progress cancellation from swallowing them.
    """
    return anyio.CancelScope(shield=True)
