"""Phase 2 acceptance: the interrupt primitive and RunSession live interrupt."""

from __future__ import annotations

import unittest

import anyio

from knuth.core.events import InferenceAborted, InferenceGenerationCompleted
from knuth.core.interrupts import InterruptSignal
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth.core.types import RunStatus
from knuth_llmd import InferenceConfig
from knuth_runtime import MemoryRunLedger, build_memory_runtime
from knuth_runtime.interrupts import InterruptController


class InterruptControllerTests(unittest.TestCase):
    def test_interrupt_is_one_shot_and_keeps_first_reason(self) -> None:
        controller = InterruptController(run_id="run-1")
        self.assertFalse(controller.interrupted)
        self.assertTrue(controller.interrupt("user_stop"))
        self.assertFalse(controller.interrupt("timeout"))
        self.assertTrue(controller.interrupted)
        self.assertEqual(controller.reason, "user_stop")
        self.assertIsNotNone(controller.created_at)

    def test_satisfies_interrupt_signal_protocol(self) -> None:
        controller = InterruptController()
        self.assertIsInstance(controller.signal, InterruptSignal)

    def test_parent_interrupt_propagates_to_child_not_reverse(self) -> None:
        parent = InterruptController()
        child = parent.child()
        child.interrupt("user_stop")
        self.assertTrue(child.interrupted)
        self.assertFalse(parent.interrupted)

        parent2 = InterruptController()
        child2 = parent2.child()
        parent2.interrupt("shutdown")
        self.assertTrue(child2.interrupted)
        self.assertEqual(child2.reason, "shutdown")

    def test_child_created_after_interrupt_inherits_it(self) -> None:
        parent = InterruptController()
        parent.interrupt("timeout")
        child = parent.child()
        self.assertTrue(child.interrupted)
        self.assertEqual(child.reason, "timeout")

    def test_wait_interrupted_is_woken_by_interrupt(self) -> None:
        async def scenario() -> bool:
            controller = InterruptController()
            woke = [False]

            async def waiter() -> None:
                await controller.wait_interrupted()
                woke[0] = True

            async with anyio.create_task_group() as tg:
                tg.start_soon(waiter)
                await anyio.sleep(0.01)
                controller.interrupt("user_stop")
            return woke[0]

        self.assertTrue(anyio.run(scenario))

    def test_register_wakeup_fires_callback_on_interrupt(self) -> None:
        controller = InterruptController()
        fired = []
        token = controller.register_wakeup(lambda: fired.append(1))
        controller.interrupt("user_stop")
        self.assertEqual(fired, [1])
        # Unregister is idempotent and safe post-interrupt.
        controller.unregister_wakeup(token)

    def test_register_wakeup_after_interrupt_fires_immediately(self) -> None:
        controller = InterruptController()
        controller.interrupt("user_stop")
        fired = []
        controller.register_wakeup(lambda: fired.append(1))
        self.assertEqual(fired, [1])


class _AbortingClient:
    """Models a request that observes the signal and aborts cooperatively."""

    model = "aborting"

    def __init__(self, reason: str = "user_stop") -> None:
        self._reason = reason

    async def stream(self, messages, tools, config, runtime=None):
        yield InferenceAborted(
            generation_id="g1", seq=1, run_id=config.run_id, reason=self._reason
        )


class _WaitingThenAbortingClient:
    """Blocks on the signal (TTFT-style) and aborts when it fires."""

    model = "waiting"

    async def stream(self, messages, tools, config, runtime=None):
        signal: InterruptSignal = runtime.abort_signal
        await signal.wait_interrupted()
        yield InferenceAborted(
            generation_id="g1", seq=1, run_id=config.run_id, reason=signal.reason
        )


class ModelAbortSafePointTests(unittest.TestCase):
    def test_model_abort_enters_interrupted_not_paused(self) -> None:
        async def scenario():
            runtime = build_memory_runtime(
                inference_client=_AbortingClient(),
                inference_config=InferenceConfig(),
                ledger=MemoryRunLedger(),
            )
            async with runtime.start("hello") as session:
                result = await session.result()
                events = await runtime.events(session.run_id)
            return result, events

        result, events = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.INTERRUPTED)
        types = [e.type for e in events]
        self.assertIn("model.aborted", types)
        self.assertIn("run.interrupted", types)
        # Active model abort carries a user-stop notice for the next turn.
        notices = [e for e in events if e.type == "conversation.notice"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].kind, "interrupted")
        self.assertNotIn("run.paused", types)
        interrupted = [e for e in events if e.type == "run.interrupted"]
        self.assertEqual(interrupted[0].active_phase, "model")

    def test_live_interrupt_wakes_blocking_model_await(self) -> None:
        async def scenario():
            runtime = build_memory_runtime(
                inference_client=_WaitingThenAbortingClient(),
                inference_config=InferenceConfig(),
                ledger=MemoryRunLedger(),
            )
            async with runtime.start("hello") as session:
                # The model await is blocked until we interrupt it.
                await anyio.sleep(0.02)
                self.assertTrue(session.interrupt("user_stop"))
                result = await session.result()
            return result

        result = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.INTERRUPTED)

    def test_normal_completion_does_not_write_interrupt(self) -> None:
        async def scenario():
            class _OkClient:
                model = "ok"

                async def stream(self, messages, tools, config, runtime=None):
                    yield InferenceGenerationCompleted(
                        generation_id="g1",
                        seq=1,
                        run_id=config.run_id,
                        message=InferenceMessage(
                            role=InferenceRole.ASSISTANT, content="done"
                        ),
                    )

            runtime = build_memory_runtime(
                inference_client=_OkClient(),
                inference_config=InferenceConfig(),
                ledger=MemoryRunLedger(),
            )
            async with runtime.start("hello") as session:
                result = await session.result()
                events = await runtime.events(session.run_id)
            return result, events

        result, events = anyio.run(scenario)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertNotIn("run.interrupted", [e.type for e in events])


if __name__ == "__main__":
    unittest.main()
