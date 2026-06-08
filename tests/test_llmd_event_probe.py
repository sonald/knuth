from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from pathlib import Path

from rich.console import Console

from knuth.core.events import (
    InferenceContentDelta,
    InferenceGenerationCompleted,
    InferenceGenerationStarted,
)
from knuth.core.messages import InferenceMessage, InferenceRole


def _load_probe_module():
    path = Path(__file__).parents[1] / "scripts" / "llmd_event_probe.py"
    spec = importlib.util.spec_from_file_location("llmd_event_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load llmd_event_probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe_module()


class LlmdEventProbeTests(unittest.TestCase):
    def test_event_json_data_includes_typed_event_fields(self) -> None:
        event = InferenceContentDelta(
            generation_id="gen-1",
            seq=2,
            run_id="run-1",
            delta="hello",
        )

        data = probe.event_json_data(event)

        self.assertEqual(data["type"], "inference.content.delta")
        self.assertEqual(data["generation_id"], "gen-1")
        self.assertEqual(data["seq"], 2)
        self.assertEqual(data["run_id"], "run-1")
        self.assertEqual(data["delta"], "hello")

    def test_render_event_prints_events_in_receive_order(self) -> None:
        console = Console(file=io.StringIO(), record=True, width=120)
        events = [
            InferenceGenerationStarted(
                generation_id="gen-1",
                seq=1,
                run_id="run-1",
                model="test-model",
            ),
            InferenceGenerationCompleted(
                generation_id="gen-1",
                seq=2,
                run_id="run-1",
                message=InferenceMessage(
                    role=InferenceRole.ASSISTANT,
                    content="done",
                ),
                finish_reason="stop",
            ),
        ]

        for index, event in enumerate(events, start=1):
            probe.render_event(console, event, index)

        output = console.export_text()
        self.assertLess(
            output.index("#1 inference.generation.started"),
            output.index("#2 inference.generation.completed"),
        )
        self.assertIn('"model": "test-model"', output)
        self.assertIn('"finish_reason": "stop"', output)


if __name__ == "__main__":
    unittest.main()
