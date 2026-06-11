from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Protocol

import anyio

from knuth.core.events import RuntimeEvent
from knuth.core.types import EventDurability


class RuntimeEventOverflowPolicy(StrEnum):
    BLOCK = "block"
    DROP_NEWEST = "drop_newest"
    DISABLE = "disable"


@dataclass(frozen=True)
class RuntimeEventInterest:
    types: frozenset[str] = frozenset()
    prefixes: frozenset[str] = frozenset()
    include_durable: bool = True
    include_transient: bool = True

    @classmethod
    def all(
        cls,
        *,
        include_durable: bool = True,
        include_transient: bool = True,
    ) -> RuntimeEventInterest:
        return cls(
            include_durable=include_durable,
            include_transient=include_transient,
        )

    @classmethod
    def for_types(
        cls,
        *types: str,
        include_durable: bool = True,
        include_transient: bool = True,
    ) -> RuntimeEventInterest:
        return cls(
            types=frozenset(types),
            include_durable=include_durable,
            include_transient=include_transient,
        )

    @classmethod
    def for_prefixes(
        cls,
        *prefixes: str,
        include_durable: bool = True,
        include_transient: bool = True,
    ) -> RuntimeEventInterest:
        return cls(
            prefixes=frozenset(prefixes),
            include_durable=include_durable,
            include_transient=include_transient,
        )

    def matches(self, event: RuntimeEvent) -> bool:
        if event.durability == EventDurability.DURABLE and not self.include_durable:
            return False
        if event.durability == EventDurability.TRANSIENT and not self.include_transient:
            return False
        if not self.types and not self.prefixes:
            return True
        if event.type in self.types:
            return True
        return any(event.type.startswith(prefix) for prefix in self.prefixes)


class RuntimeEventListener(Protocol):
    @property
    def interest(self) -> RuntimeEventInterest:
        ...

    async def handle_event(self, event: RuntimeEvent) -> None:
        ...


@dataclass(frozen=True)
class RuntimeListenerFailure:
    listener_name: str
    error: BaseException
    required: bool


class RuntimeObservationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        result: object | None = None,
        failures: tuple[RuntimeListenerFailure, ...],
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.result = result
        self.failures = failures


@dataclass(frozen=True)
class ListenerStats:
    handled: int = 0
    dropped: int = 0
    failed: bool = False
    disabled: bool = False


@dataclass
class _MutableListenerStats:
    """Mutable mirror of :class:`ListenerStats`; same fields by construction."""

    handled: int = 0
    dropped: int = 0
    failed: bool = False
    disabled: bool = False

    def snapshot(self) -> ListenerStats:
        return ListenerStats(**asdict(self))


@dataclass
class _ListenerBinding:
    listener: RuntimeEventListener
    send: anyio.abc.ObjectSendStream[RuntimeEvent]
    receive: anyio.abc.ObjectReceiveStream[RuntimeEvent]
    done: anyio.Event
    stats: _MutableListenerStats = field(default_factory=_MutableListenerStats)
    active: bool = True

    @property
    def required(self) -> bool:
        return bool(getattr(self.listener, "required", False))

    @property
    def overflow_policy(self) -> RuntimeEventOverflowPolicy:
        return getattr(
            self.listener,
            "overflow_policy",
            RuntimeEventOverflowPolicy.BLOCK,
        )

    @property
    def name(self) -> str:
        return self.listener.__class__.__name__


class ListenerHandle:
    def __init__(self, hub: LiveRuntimeObservation, binding: _ListenerBinding) -> None:
        self._hub = hub
        self._binding = binding

    async def remove(self) -> None:
        await self._hub.remove(self._binding)

    @property
    def stats(self) -> ListenerStats:
        return self._binding.stats.snapshot()


class LiveRuntimeObservation:
    def __init__(self, task_group: anyio.abc.TaskGroup, *, drain_timeout: float = 2.0):
        self._task_group = task_group
        self._drain_timeout = drain_timeout
        self._bindings: list[_ListenerBinding] = []
        self._required_failures: list[RuntimeListenerFailure] = []

    async def add_listener(self, listener: RuntimeEventListener) -> ListenerHandle:
        buffer_size = int(getattr(listener, "buffer_size", 100))
        send, receive = anyio.create_memory_object_stream[RuntimeEvent](buffer_size)
        binding = _ListenerBinding(
            listener=listener,
            send=send,
            receive=receive,
            done=anyio.Event(),
        )
        self._bindings.append(binding)
        self._task_group.start_soon(self._drain, binding)
        return ListenerHandle(self, binding)

    async def remove(self, binding: _ListenerBinding) -> None:
        if not binding.active:
            return
        binding.active = False
        binding.stats.disabled = True
        await binding.send.aclose()

    async def publish(self, event: RuntimeEvent) -> None:
        for binding in tuple(self._bindings):
            if not binding.active:
                continue
            if not binding.listener.interest.matches(event):
                continue
            if binding.overflow_policy == RuntimeEventOverflowPolicy.BLOCK:
                try:
                    await binding.send.send(event)
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    await self.remove(binding)
                continue
            try:
                binding.send.send_nowait(event)
            except anyio.WouldBlock:
                if binding.overflow_policy == RuntimeEventOverflowPolicy.DROP_NEWEST:
                    binding.stats.dropped += 1
                elif binding.overflow_policy == RuntimeEventOverflowPolicy.DISABLE:
                    await self.remove(binding)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                await self.remove(binding)

    async def aclose(self) -> None:
        for binding in tuple(self._bindings):
            if binding.active:
                binding.active = False
                await binding.send.aclose()
        with anyio.move_on_after(self._drain_timeout):
            for binding in tuple(self._bindings):
                await binding.done.wait()

    @property
    def required_failures(self) -> tuple[RuntimeListenerFailure, ...]:
        return tuple(self._required_failures)

    def stats(self) -> dict[RuntimeEventListener, ListenerStats]:
        return {binding.listener: binding.stats.snapshot() for binding in self._bindings}

    async def _drain(self, binding: _ListenerBinding) -> None:
        try:
            async with binding.receive:
                async for event in binding.receive:
                    try:
                        await binding.listener.handle_event(event)
                    except Exception as error:
                        binding.stats.failed = True
                        binding.stats.disabled = True
                        binding.active = False
                        failure = RuntimeListenerFailure(
                            listener_name=binding.name,
                            error=error,
                            required=binding.required,
                        )
                        if binding.required:
                            self._required_failures.append(failure)
                        await binding.send.aclose()
                        break
                    else:
                        binding.stats.handled += 1
        finally:
            binding.done.set()
