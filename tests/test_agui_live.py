"""Phase 5 acceptance: AG-UI live run manager.

SSE is a subscription, not the run lifecycle: disconnect unsubscribes, explicit
stop interrupts, a stuck tool is force-cleaned after the deadline, and a
duplicate active prompt is rejected.
"""

from __future__ import annotations

import unittest

import anyio

from knuth.core.events import InferenceAborted, InferenceGenerationCompleted
from knuth.core.interrupts import InterruptSignal
from knuth.core.invocations import ToolEffect, ToolInvocationStatus, ToolRisk
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig
from knuth_runtime import MemoryRunLedger, build_memory_runtime
from knuth_toold import AllowAllPolicy, ToolBroker, ToolManifest, ToolRegistry
from knuth_agui.live import DuplicateActivePromptError, LiveRunManager


class _WaitingModel:
    """Blocks on the interrupt signal, then aborts cooperatively."""

    model = "waiting"

    async def stream(self, messages, tools, config, runtime=None):
        signal: InterruptSignal = runtime.abort_signal
        await signal.wait_interrupted()
        yield InferenceAborted(
            generation_id="g1", seq=1, run_id=config.run_id, reason=signal.reason
        )


class _OkModel:
    model = "ok"

    async def stream(self, messages, tools, config, runtime=None):
        yield InferenceGenerationCompleted(
            generation_id="g1",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(role=InferenceRole.ASSISTANT, content="done"),
        )


class _ToolModel:
    model = "tool"

    async def stream(self, messages, tools, config, runtime=None):
        yield InferenceGenerationCompleted(
            generation_id="g1",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT,
                tool_calls=[CoreToolCall(tool_call_id="c1", name="stuck", arguments={})],
            ),
        )


class _StuckTool:
    """Never checks the signal and never returns — a tool stuck off-safe-point."""

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="stuck",
            description="never returns",
            parameters={"type": "object", "properties": {}},
            effect=ToolEffect.EXTERNAL_WRITE,
            risk=ToolRisk.HIGH,
        )

    async def invoke(self, invocation, ctx):
        await anyio.sleep(100)


class _Provider:
    name = "test"

    def __init__(self, *tools):
        self._tools = {t.manifest.name: t for t in tools}

    async def list_tools(self):
        return [t.manifest for t in self._tools.values()]

    async def call_tool(self, invocation, ctx):
        return await self._tools[invocation.tool_name].invoke(invocation, ctx)


def _runtime(model, *tools):
    if tools:
        registry = ToolRegistry()
        registry.add_provider(_Provider(*tools))
        # Allow the stuck tool to actually run (no approval gate) so we can
        # exercise the force-cancel + recovery path.
        broker = ToolBroker(registry, AllowAllPolicy())
    else:
        broker = None
    return build_memory_runtime(
        inference_client=model,
        inference_config=InferenceConfig(),
        ledger=MemoryRunLedger(),
        tool_broker=broker,
        include_default_tools=not tools,
    )


def _factory(runtime, prompt, run_id):
    async def build_factory():
        def make(listener):
            return runtime.start(prompt, run_id=run_id, listeners=[listener])

        return make

    return build_factory


class LiveManagerTests(unittest.TestCase):
    def test_disconnect_unsubscribes_but_run_continues(self) -> None:
        async def scenario():
            runtime = _runtime(_WaitingModel())
            manager = LiveRunManager(runtime, deadline_s=5.0)
            async with anyio.create_task_group() as tg:
                manager.bind(tg)
                live, sub_a = await manager.start_or_attach(
                    "run_a", prompt="go", build_factory=_factory(runtime, "go", "run_a")
                )
                # A second viewer attaches without a prompt.
                attached = manager.attach_if_live("run_a")
                self.assertIsNotNone(attached)
                _, sub_b = attached
                # Viewer A "disconnects": unsubscribe + close. Run must continue.
                live.fanout.remove(sub_a)
                await sub_a.aclose()
                await anyio.sleep(0.02)
                # The run is still live (model blocked on the signal).
                self.assertTrue(manager.is_live("run_a"))
                # Now stop it explicitly.
                await manager.interrupt("run_a")
                await live.finished.wait()
                tg.cancel_scope.cancel()
            return await runtime.status("run_a")

        status = anyio.run(scenario)
        self.assertEqual(status, RunStatus.INTERRUPTED)

    def test_duplicate_active_prompt_is_rejected(self) -> None:
        async def scenario():
            runtime = _runtime(_WaitingModel())
            manager = LiveRunManager(runtime, deadline_s=5.0)
            async with anyio.create_task_group() as tg:
                manager.bind(tg)
                live, _ = await manager.start_or_attach(
                    "run_b", prompt="go", build_factory=_factory(runtime, "go", "run_b")
                )
                raised = False
                try:
                    await manager.start_or_attach(
                        "run_b",
                        prompt="second",
                        build_factory=_factory(runtime, "second", "run_b"),
                    )
                except DuplicateActivePromptError:
                    raised = True
                await manager.interrupt("run_b")
                await live.finished.wait()
                tg.cancel_scope.cancel()
            return raised

        self.assertTrue(anyio.run(scenario))

    def test_explicit_stop_interrupts_active_run(self) -> None:
        async def scenario():
            runtime = _runtime(_WaitingModel())
            manager = LiveRunManager(runtime, deadline_s=5.0)
            async with anyio.create_task_group() as tg:
                manager.bind(tg)
                live, _ = await manager.start_or_attach(
                    "run_c", prompt="go", build_factory=_factory(runtime, "go", "run_c")
                )
                await anyio.sleep(0.02)
                stopped = await manager.interrupt("run_c")
                await live.finished.wait()
                tg.cancel_scope.cancel()
            return stopped, await runtime.status("run_c")

        stopped, status = anyio.run(scenario)
        self.assertTrue(stopped)
        self.assertEqual(status, RunStatus.INTERRUPTED)

    def test_stop_with_no_live_session_is_noop(self) -> None:
        async def scenario():
            runtime = _runtime(_OkModel())
            manager = LiveRunManager(runtime, deadline_s=5.0)
            async with anyio.create_task_group() as tg:
                manager.bind(tg)
                stopped = await manager.interrupt("run_missing")
                tg.cancel_scope.cancel()
            return stopped

        self.assertFalse(anyio.run(scenario))

    def test_deadline_force_cleanup_recovers_stuck_tool(self) -> None:
        async def scenario():
            runtime = _runtime(_ToolModel(), _StuckTool())
            manager = LiveRunManager(runtime, deadline_s=0.2)
            async with anyio.create_task_group() as tg:
                manager.bind(tg)
                live, _ = await manager.start_or_attach(
                    "run_d", prompt="go", build_factory=_factory(runtime, "go", "run_d")
                )
                # Wait until the stuck tool is running.
                await anyio.sleep(0.1)
                await manager.interrupt("run_d")
                # Tool ignores the signal; deadline force-cancels, then recovery.
                await live.finished.wait()
                tg.cancel_scope.cancel()
            status = await runtime.status("run_d")
            inv = await runtime._services.ledger.get_invocation("c1")
            return status, inv.status

        status, inv_status = anyio.run(scenario)
        # Force-cancel left no zombie RUNNING: recovery paused the run and
        # marked the external-write invocation UNKNOWN.
        self.assertEqual(status, RunStatus.PAUSED)
        self.assertEqual(inv_status, ToolInvocationStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
