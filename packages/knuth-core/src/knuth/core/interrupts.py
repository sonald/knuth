from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class InterruptSignal(Protocol):
    """Live, normalized control signal observed by model, tool, and UI layers.

    It is one-shot and sticky: once interrupted it stays interrupted and keeps
    its first ``reason``. Execution cancellation (AnyIO cancel scope, provider
    abort, subprocess terminate) is only the backing mechanism — layers observe
    this signal at their own safe points and decide cooperatively how to stop.

    ``checkpoint()`` only yields control; it never raises a cancellation. Poll
    ``interrupted`` after it. ``wait_interrupted()`` lets a single blocking await
    (model TTFT, subprocess, long network read) be woken instead of polled.
    """

    @property
    def interrupted(self) -> bool:
        ...

    @property
    def reason(self) -> str | None:
        ...

    async def checkpoint(self) -> None:
        ...

    async def wait_interrupted(self) -> None:
        ...
