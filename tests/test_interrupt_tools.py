"""Phase 3 acceptance: cooperative model and tool interrupt outcomes."""

from __future__ import annotations

import unittest

import anyio

from knuth.core.events import InferenceAborted
from knuth.core.invocations import (
    ToolCallDecision,
    ToolEffect,
    ToolInvocation,
    ToolInvocationStatus,
    ToolRisk,
    args_hash_for,
)
from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall as CoreToolCall
from knuth.core.tools import ToolExecutionOutcome, ToolResult
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig, InferenceRuntimeOptions
from knuth_llmd.client import LiteLLMInferenceClient
from knuth_runtime import MemoryRunLedger, build_memory_runtime
from knuth_runtime.interrupts import InterruptController
from knuth_runtime.policy import PolicyEngine
from knuth_toold import (
    ToolBroker,
    ToolExecutionResult,
    ToolManifest,
    ToolRegistry,
    ToolRuntimeContext,
)


def _invocation(name: str, args: dict | None = None) -> ToolInvocation:
    args = args or {}
    return ToolInvocation(
        tool_call_id="c1",
        run_id="run-1",
        batch_id="b1",
        step_id="s1",
        tool_name=name,
        args=args,
        args_hash=args_hash_for(args),
        effect=ToolEffect.DANGEROUS,
    )


class _LongRunningTool:
    """Polls the signal and returns interrupted when it fires."""

    def __init__(
        self,
        name: str = "long",
        effect: ToolEffect = ToolEffect.LOCAL_WRITE,
        interrupted_message: str = "long tool stopped cooperatively at a safe point",
    ):
        self._name = name
        self._effect = effect
        self._interrupted_message = interrupted_message

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name=self._name,
            description="runs until interrupted",
            parameters={"type": "object", "properties": {}},
            effect=self._effect,
            risk=ToolRisk.MEDIUM,
        )

    async def invoke(self, invocation, ctx: ToolRuntimeContext):
        signal = ctx.interrupt_signal
        for _ in range(1000):
            if signal is not None and signal.interrupted:
                return ToolExecutionResult.interrupted(self._interrupted_message)
            await anyio.sleep(0.005)
        return ToolResult.success(content="finished")


class _SelfWakingTool:
    """A single-blocking tool that binds the signal to wake its own await.

    This is the design's blocking-tool pattern (like the shell tool): it does
    not poll, it waits on ``wait_interrupted`` and reports its own outcome —
    the broker never preempts it with a cancel scope.
    """

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="blocking",
            description="single blocking await woken by the signal",
            parameters={"type": "object", "properties": {}},
            effect=ToolEffect.LOCAL_WRITE,
            risk=ToolRisk.MEDIUM,
        )

    async def invoke(self, invocation, ctx: ToolRuntimeContext):
        signal = ctx.interrupt_signal
        done = anyio.Event()
        async with anyio.create_task_group() as tg:

            async def _watch() -> None:
                await signal.wait_interrupted()
                done.set()
                tg.cancel_scope.cancel()

            async def _work() -> None:
                await done.wait()  # never completes on its own here

            tg.start_soon(_watch)
            tg.start_soon(_work)
        return ToolExecutionResult.interrupted(
            "blocking tool woke on the signal and stopped cleanly"
        )


class _BlockingTool:
    """Blocks forever, ignoring the signal — only force-cancel stops it."""

    def __init__(self, name: str, effect: ToolEffect):
        self._name = name
        self._effect = effect

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name=self._name,
            description="blocks forever",
            parameters={"type": "object", "properties": {}},
            effect=self._effect,
            risk=ToolRisk.HIGH,
        )

    async def invoke(self, invocation, ctx: ToolRuntimeContext):
        await anyio.sleep(100)


class _RaisingOnInterruptTool:
    """Raises (e.g. cancellation) when interrupted, with no reliable outcome."""

    def __init__(self, name: str, effect: ToolEffect):
        self._name = name
        self._effect = effect

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name=self._name,
            description="raises on interrupt",
            parameters={"type": "object", "properties": {}},
            effect=self._effect,
            risk=ToolRisk.HIGH,
        )

    async def invoke(self, invocation, ctx: ToolRuntimeContext):
        signal = ctx.interrupt_signal
        while not (signal is not None and signal.interrupted):
            await anyio.sleep(0.005)
        raise RuntimeError("lost contact with the side effect")


class _Provider:
    name = "test"

    def __init__(self, *tools):
        self._tools = {t.manifest.name: t for t in tools}

    async def list_tools(self):
        return [t.manifest for t in self._tools.values()]

    async def call_tool(self, invocation, ctx):
        return await self._tools[invocation.tool_name].invoke(invocation, ctx)


def _broker(*tools) -> ToolBroker:
    registry = ToolRegistry()
    registry.add_provider(_Provider(*tools))
    return ToolBroker(registry, PolicyEngine())


class BrokerOutcomeTests(unittest.TestCase):
    def test_long_tool_returns_interrupted_after_signal(self) -> None:
        async def scenario():
            controller = InterruptController()
            broker = _broker(_LongRunningTool())
            await broker.registry.refresh()
            result = [None]

            async with anyio.create_task_group() as tg:

                async def run():
                    result[0] = await broker.execute(
                        _invocation("long"), signal=controller.signal
                    )

                tg.start_soon(run)
                await anyio.sleep(0.02)
                controller.interrupt("user_stop")
            return result[0]

        result = anyio.run(scenario)
        self.assertEqual(result.outcome, ToolExecutionOutcome.INTERRUPTED)
        self.assertIn("cooperatively", result.observation or "")

    def test_self_waking_blocking_tool_reports_interrupted(self) -> None:
        async def scenario():
            controller = InterruptController()
            broker = _broker(_SelfWakingTool())
            await broker.registry.refresh()
            result = [None]

            async with anyio.create_task_group() as tg:

                async def run():
                    result[0] = await broker.execute(
                        _invocation("blocking"), signal=controller.signal
                    )

                tg.start_soon(run)
                await anyio.sleep(0.02)
                controller.interrupt("user_stop")
            return result[0]

        result = anyio.run(scenario)
        self.assertEqual(result.outcome, ToolExecutionOutcome.INTERRUPTED)
        self.assertIn("cleanly", result.observation or "")

    def test_real_cancellation_propagates_not_forged_into_outcome(self) -> None:
        """A force-stop cancellation must propagate, not be converted into a
        clean outcome (ADR-007 §9). Recovery — not the broker — settles it."""

        async def scenario():
            controller = InterruptController()
            broker = _broker(_BlockingTool("danger", ToolEffect.EXTERNAL_WRITE))
            await broker.registry.refresh()
            controller.interrupt("user_stop")  # signal already set, like a stop
            propagated = [False]
            result = [None]

            async with anyio.create_task_group() as tg:

                async def run():
                    try:
                        result[0] = await broker.execute(
                            _invocation("danger"), signal=controller.signal
                        )
                    except anyio.get_cancelled_exc_class():
                        propagated[0] = True
                        raise

                tg.start_soon(run)
                await anyio.sleep(0.02)
                # Force-stop: cancel the task group while the tool blocks.
                tg.cancel_scope.cancel()
            return propagated[0], result[0]

        propagated, result = anyio.run(scenario)
        # The broker neither swallowed the cancellation nor forged an outcome.
        self.assertTrue(propagated)
        self.assertIsNone(result)

    def test_dangerous_tool_with_no_outcome_becomes_unknown(self) -> None:
        async def scenario():
            controller = InterruptController()
            broker = _broker(
                _RaisingOnInterruptTool("danger", ToolEffect.EXTERNAL_WRITE)
            )
            await broker.registry.refresh()
            result = [None]

            async with anyio.create_task_group() as tg:

                async def run():
                    result[0] = await broker.execute(
                        _invocation("danger"), signal=controller.signal
                    )

                tg.start_soon(run)
                await anyio.sleep(0.02)
                controller.interrupt("user_stop")
            return result[0]

        result = anyio.run(scenario)
        self.assertEqual(result.outcome, ToolExecutionOutcome.UNKNOWN)

    def test_readonly_tool_with_no_outcome_defaults_to_interrupted(self) -> None:
        async def scenario():
            controller = InterruptController()
            broker = _broker(_RaisingOnInterruptTool("reader", ToolEffect.READ))
            await broker.registry.refresh()
            result = [None]

            async with anyio.create_task_group() as tg:

                async def run():
                    result[0] = await broker.execute(
                        _invocation("reader"), signal=controller.signal
                    )

                tg.start_soon(run)
                await anyio.sleep(0.02)
                controller.interrupt("user_stop")
            return result[0]

        result = anyio.run(scenario)
        # A user stop is not the tool's own failure.
        self.assertEqual(result.outcome, ToolExecutionOutcome.INTERRUPTED)


class LoopToolInterruptTests(unittest.TestCase):
    def _build(self, tool):
        registry = ToolRegistry()
        registry.add_provider(_Provider(tool))
        broker = ToolBroker(registry, PolicyEngine())
        return build_memory_runtime(
            inference_client=_ToolThenNothingClient(tool.manifest.name),
            inference_config=InferenceConfig(),
            ledger=MemoryRunLedger(),
            tool_broker=broker,
        )

    def test_active_tool_interrupt_collapses_batch_to_interrupted(self) -> None:
        async def scenario():
            runtime = self._build(_LongRunningTool(effect=ToolEffect.READ))
            async with runtime.start("go") as session:
                await anyio.sleep(0.03)
                session.interrupt("user_stop")
                result = await session.result()
                events = await runtime.events(session.run_id)
            return result, events

        result, events = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.INTERRUPTED)
        types = [e.type for e in events]
        self.assertIn("tool.batch_closed", types)
        self.assertIn("run.interrupted", types)
        completed = [e for e in events if e.type == "tool.invocation_completed"]
        self.assertTrue(completed)
        self.assertEqual(completed[-1].outcome, "interrupted")

    def test_active_tool_interrupt_runs_redaction_checkpoint(self) -> None:
        big = "interrupted observation " + ("x" * 5000)

        async def scenario():
            runtime = self._build(
                _LongRunningTool(effect=ToolEffect.READ, interrupted_message=big)
            )
            async with runtime.start("go") as session:
                await anyio.sleep(0.03)
                session.interrupt("user_stop")
                result = await session.result()
                events = await runtime.events(session.run_id)
                context = await runtime.model_context_messages(session.run_id)
            return result, events, context

        result, events, context = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.INTERRUPTED)
        self.assertIn("message.rewrite_anchor", [e.type for e in events])
        tool_results = [
            message
            for message in context
            if message.role == InferenceRole.TOOL_RESULT
        ]
        self.assertTrue(
            (tool_results[0].content or "").startswith(
                "Result redacted for context headroom."
            )
        )
        self.assertNotIn(big, tool_results[0].content or "")

    def test_unstarted_calls_are_abandoned_on_interrupt(self) -> None:
        async def scenario():
            tool = _LongRunningTool(effect=ToolEffect.READ)
            registry = ToolRegistry()
            registry.add_provider(_Provider(tool))
            broker = ToolBroker(registry, PolicyEngine())
            runtime = build_memory_runtime(
                inference_client=_TwoToolClient("long"),
                inference_config=InferenceConfig(),
                ledger=MemoryRunLedger(),
                tool_broker=broker,
            )
            async with runtime.start("go") as session:
                await anyio.sleep(0.03)
                session.interrupt("user_stop")
                result = await session.result()
                events = await runtime.events(session.run_id)
            return result, events

        result, events = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.INTERRUPTED)
        completed = {
            e.tool_call_id: e
            for e in events
            if e.type == "tool.invocation_completed"
        }
        # Both calls observed; the unstarted one is abandoned.
        self.assertEqual(completed["c1"].outcome, "interrupted")
        self.assertEqual(completed["c2"].outcome, "interrupted")
        self.assertEqual(completed["c2"].tool_status, "abandoned")


class _ToolThenNothingClient:
    model = "tool-client"

    def __init__(self, tool_name: str):
        self._tool_name = tool_name
        self.calls = 0

    async def stream(self, messages, tools, config, runtime=None):
        self.calls += 1
        from knuth.core.events import InferenceGenerationCompleted

        yield InferenceGenerationCompleted(
            generation_id="g1",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT,
                tool_calls=[
                    CoreToolCall(tool_call_id="c1", name=self._tool_name, arguments={})
                ],
            ),
        )


class _TwoToolClient:
    model = "two-tool"

    def __init__(self, tool_name: str):
        self._tool_name = tool_name

    async def stream(self, messages, tools, config, runtime=None):
        from knuth.core.events import InferenceGenerationCompleted

        yield InferenceGenerationCompleted(
            generation_id="g1",
            seq=1,
            run_id=config.run_id,
            message=InferenceMessage(
                role=InferenceRole.ASSISTANT,
                tool_calls=[
                    CoreToolCall(tool_call_id="c1", name=self._tool_name, arguments={}, index=0),
                    CoreToolCall(tool_call_id="c2", name=self._tool_name, arguments={}, index=1),
                ],
            ),
        )


class ShellInterruptTests(unittest.TestCase):
    def test_shell_interrupt_warns_about_partial_side_effects(self) -> None:
        import tempfile
        from pathlib import Path

        from knuth_toold.builtins import ShellTool

        async def scenario(offload_root):
            controller = InterruptController()
            tool = ShellTool(offload_root=offload_root, interrupt_grace_s=0.2)
            ctx = ToolRuntimeContext(
                run_id="run-1",
                tool_call_id="c1",
                interrupt_signal=controller.signal,
            )
            inv = _invocation("shell", {"command": "printf started; sleep 5"})
            result = [None]

            async with anyio.create_task_group() as tg:

                async def run():
                    result[0] = await tool.invoke(inv, ctx)

                tg.start_soon(run)
                await anyio.sleep(0.05)
                controller.interrupt("user_stop")
            return result[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir) / "offload")

        self.assertEqual(result.outcome, ToolExecutionOutcome.INTERRUPTED)
        self.assertIn("side effects", result.observation or "")


class LlmdAbortReasonTests(unittest.TestCase):
    def test_pretoken_interrupt_aborts_with_signal_reason(self) -> None:
        async def scenario():
            controller = InterruptController()

            async def completion_fn(**kwargs):
                # Never resolves on its own — only the interrupt frees it.
                await anyio.sleep(100)

            client = LiteLLMInferenceClient(
                model="x", completion_fn=completion_fn
            )
            options = InferenceRuntimeOptions(abort_signal=controller.signal)
            events = []

            async with anyio.create_task_group() as tg:

                async def run():
                    async for event in client.stream(
                        messages=[], tools=[], config=InferenceConfig(run_id="run-1"),
                        runtime=options,
                    ):
                        events.append(event)

                tg.start_soon(run)
                await anyio.sleep(0.02)
                controller.interrupt("timeout")
            return events

        events = anyio.run(scenario)
        aborted = [e for e in events if isinstance(e, InferenceAborted)]
        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0].reason, "timeout")


if __name__ == "__main__":
    unittest.main()
