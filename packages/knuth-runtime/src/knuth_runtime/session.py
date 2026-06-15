from __future__ import annotations

from collections.abc import Iterable
from contextlib import AsyncExitStack
from typing import Self

import anyio

from knuth.core.events import (
    RunInvocationEndedDraft,
    RunInvocationStartedDraft,
    RunResumedDraft,
    UserMessageDraft,
)
from knuth.core.types import ErrorInfo, RunStatus
from knuth_llmd import InferenceConfig, InferenceRuntimeOptions

from knuth_runtime.invocation import RunInvocationMode, RuntimeInvocation
from knuth_runtime.loop import run_agent_loop
from knuth_runtime.observation import (
    ListenerHandle,
    LiveRuntimeObservation,
    RuntimeEventListener,
    RuntimeObservationError,
)
from knuth_runtime.result import RunResult, answer_from_events
from knuth_runtime.services import RuntimeServices

_RESUMABLE_STATUSES = frozenset(
    {
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_TOOL_RESULT,
        RunStatus.PAUSED,
    }
)


class RunSession:
    def __init__(
        self,
        *,
        mode: RunInvocationMode,
        services: RuntimeServices,
        inference_config: InferenceConfig,
        prompt: str | None = None,
        run_id: str | None = None,
        listeners: Iterable[RuntimeEventListener] = (),
        runtime_options: InferenceRuntimeOptions | None = None,
    ) -> None:
        self._mode = mode
        self._services = services
        self._inference_config = inference_config
        self._prompt = prompt
        self._run_id = run_id
        self._initial_listeners = tuple(listeners)
        self._runtime_options = runtime_options
        self._exit_stack: AsyncExitStack | None = None
        self._task_group: anyio.abc.TaskGroup | None = None
        self._observation: LiveRuntimeObservation | None = None
        self._done = anyio.Event()
        self._entered = False
        self._final_result: RunResult | None = None
        self._error: BaseException | None = None

    @property
    def run_id(self) -> str:
        if self._run_id is None:
            raise RuntimeError("run_id is available after entering the session")
        return self._run_id

    @property
    def final_result(self) -> RunResult | None:
        return self._final_result

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("RunSession cannot be entered more than once")
        self._entered = True
        self._exit_stack = AsyncExitStack()
        try:
            self._task_group = await self._exit_stack.enter_async_context(
                anyio.create_task_group()
            )
            self._observation = LiveRuntimeObservation(self._task_group)
            for listener in self._initial_listeners:
                await self._observation.add_listener(listener)
            await self._prepare_run_id()
            invocation = RuntimeInvocation(
                run_id=self.run_id,
                mode=self._mode,
                services=self._services,
                observation=self._observation,
            )
            await invocation.emit(RunInvocationStartedDraft(mode=self._mode))
            await self._prepare_run(invocation)
            self._task_group.start_soon(self._drive, invocation)
        except BaseException:
            # A failed enter must still unwind the task group, or its
            # listener drain tasks would leak.
            if self._task_group is not None:
                self._task_group.cancel_scope.cancel()
            await self._exit_stack.aclose()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._task_group is None or self._exit_stack is None:
            return
        if not self._done.is_set():
            self._task_group.cancel_scope.cancel()
        elif self._observation is not None:
            await self._observation.aclose()
        await self._exit_stack.__aexit__(exc_type, exc, tb)

    async def add_listener(self, listener: RuntimeEventListener) -> ListenerHandle:
        if not self._entered or self._observation is None:
            raise RuntimeError("RunSession.add_listener() requires an active session")
        return await self._observation.add_listener(listener)

    async def result(self) -> RunResult:
        if not self._entered:
            raise RuntimeError("RunSession.result() requires an active session")
        await self._done.wait()
        if self._error is not None:
            raise self._error
        result = self._final_result
        failures = (
            self._observation.required_failures
            if self._observation is not None
            else ()
        )
        if failures:
            raise RuntimeObservationError(
                "required runtime event listener failed",
                run_id=self.run_id,
                result=result,
                failures=failures,
            )
        if result is None:
            raise RuntimeError("RunSession completed without a result")
        return result

    async def _prepare_run_id(self) -> None:
        if self._mode == "start":
            if self._prompt is None:
                raise ValueError("prompt is required to start a new run")
            run = await self._services.ledger.create_run(
                self._prompt, run_id=self._run_id
            )
            self._run_id = run.id
            return
        if self._run_id is None:
            raise ValueError("run_id is required")

    async def _prepare_run(self, invocation: RuntimeInvocation) -> None:
        if self._mode == "start":
            if self._prompt is None:
                raise RuntimeError("start session missing prompt")
            await invocation.emit(UserMessageDraft(content=self._prompt))
            return
        run = await self._services.ledger.get_run(invocation.run_id)
        if self._mode == "continue":
            if self._prompt is None:
                raise ValueError("prompt is required to continue a run")
            await invocation.emit(UserMessageDraft(content=self._prompt))
            if run.status == RunStatus.SUCCEEDED:
                await invocation.emit(RunResumedDraft(cause="user_message"))
            return
        # resume: unlock through the ledger; pending approvals make this fail
        # loudly instead of silently re-entering the loop.
        if run.status in _RESUMABLE_STATUSES:
            await invocation.emit(RunResumedDraft(cause="user_resume"))

    async def _drive(self, invocation: RuntimeInvocation) -> None:
        status: RunStatus | None = None
        error: ErrorInfo | None = None
        try:
            status = await run_agent_loop(
                invocation,
                self._inference_config,
                runtime_options=self._runtime_options,
            )
            events = await self._services.ledger.list_events(invocation.run_id)
            self._final_result = RunResult(
                answer=answer_from_events(events),
                run_id=invocation.run_id,
                status=status,
            )
        except Exception as exc:
            self._error = exc
            error = ErrorInfo(code=exc.__class__.__name__, message=str(exc))
        finally:
            await invocation.emit(
                RunInvocationEndedDraft(
                    mode=self._mode,
                    status=status,
                    error=error,
                )
            )
            if self._observation is not None:
                await self._observation.aclose()
            self._done.set()
