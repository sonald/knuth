import unittest

import anyio

from knuth.core.events import (
    ModelContentDeltaDraft,
    RunSucceededDraft,
    emit_transient_runtime_event,
    store_runtime_event,
)
from knuth_runtime.observation import (
    LiveRuntimeObservation,
    RuntimeEventInterest,
    RuntimeEventOverflowPolicy,
)


def _transient_event(event_type: str = "model.content.delta"):
    if event_type != "model.content.delta":
        raise ValueError(event_type)
    return emit_transient_runtime_event(
        "run-1",
        ModelContentDeltaDraft(delta="hello"),
        event_id="evt-transient",
        created_at="2026-06-09T00:00:00Z",
    )


def _durable_event():
    return store_runtime_event(
        "run-1",
        1,
        RunSucceededDraft(answer="done", turns=1),
        event_id="evt-durable",
        created_at="2026-06-09T00:00:01Z",
    )


class _CollectingListener:
    required = False
    buffer_size = 10
    overflow_policy = RuntimeEventOverflowPolicy.BLOCK

    def __init__(self, interest: RuntimeEventInterest) -> None:
        self.interest = interest
        self.events = []

    async def handle_event(self, event) -> None:
        self.events.append(event)


class _FailingListener:
    buffer_size = 10
    overflow_policy = RuntimeEventOverflowPolicy.BLOCK

    def __init__(self, *, required: bool) -> None:
        self.interest = RuntimeEventInterest.all()
        self.required = required

    async def handle_event(self, event) -> None:
        raise RuntimeError(f"failed on {event.type}")


class _BlockingListener:
    buffer_size = 1
    overflow_policy = RuntimeEventOverflowPolicy.DROP_NEWEST

    def __init__(self) -> None:
        self.interest = RuntimeEventInterest.all()
        self.started = anyio.Event()

    async def handle_event(self, event) -> None:
        self.started.set()
        await anyio.sleep_forever()


class RuntimeObservationTests(unittest.TestCase):
    def test_interest_matches_exact_prefix_and_durability(self) -> None:
        transient = _transient_event()
        durable = _durable_event()

        self.assertTrue(
            RuntimeEventInterest.for_types("model.content.delta").matches(transient)
        )
        self.assertTrue(RuntimeEventInterest.for_prefixes("model.").matches(transient))
        self.assertFalse(RuntimeEventInterest.for_prefixes("tool.").matches(transient))
        self.assertFalse(
            RuntimeEventInterest.all(include_transient=False).matches(transient)
        )
        self.assertFalse(RuntimeEventInterest.all(include_durable=False).matches(durable))

    def test_fanout_filters_per_listener_interest(self) -> None:
        async def scenario():
            async with anyio.create_task_group() as tg:
                hub = LiveRuntimeObservation(tg)
                model_listener = _CollectingListener(
                    RuntimeEventInterest.for_prefixes("model.")
                )
                run_listener = _CollectingListener(
                    RuntimeEventInterest.for_prefixes("run.")
                )
                await hub.add_listener(model_listener)
                await hub.add_listener(run_listener)
                await hub.publish(_transient_event())
                await hub.publish(_durable_event())
                await hub.aclose()
                return model_listener.events, run_listener.events

        model_events, run_events = anyio.run(scenario)

        self.assertEqual([event.type for event in model_events], ["model.content.delta"])
        self.assertEqual([event.type for event in run_events], ["run.succeeded"])

    def test_non_required_listener_failure_disables_listener(self) -> None:
        async def scenario():
            async with anyio.create_task_group() as tg:
                hub = LiveRuntimeObservation(tg)
                listener = _FailingListener(required=False)
                handle = await hub.add_listener(listener)
                await hub.publish(_transient_event())
                await hub.aclose()
                return handle.stats, hub.required_failures

        stats, failures = anyio.run(scenario)

        self.assertTrue(stats.failed)
        self.assertTrue(stats.disabled)
        self.assertEqual(failures, ())

    def test_required_listener_failure_is_recorded(self) -> None:
        async def scenario():
            async with anyio.create_task_group() as tg:
                hub = LiveRuntimeObservation(tg)
                await hub.add_listener(_FailingListener(required=True))
                await hub.publish(_transient_event())
                await hub.aclose()
                return hub.required_failures

        failures = anyio.run(scenario)

        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].required)
        self.assertEqual(failures[0].listener_name, "_FailingListener")

    def test_drop_newest_overflow_does_not_block_publisher(self) -> None:
        async def scenario():
            async with anyio.create_task_group() as tg:
                hub = LiveRuntimeObservation(tg)
                listener = _BlockingListener()
                handle = await hub.add_listener(listener)
                await hub.publish(_transient_event())
                await listener.started.wait()
                await hub.publish(_transient_event())
                await hub.publish(_transient_event())
                stats = handle.stats
                await handle.remove()
                tg.cancel_scope.cancel()
                return stats

        stats = anyio.run(scenario)

        self.assertEqual(stats.dropped, 1)


if __name__ == "__main__":
    unittest.main()
