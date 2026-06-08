from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import anyio
from knuth.core.events import InferenceContentDelta, InferenceGenerationStarted
from textual.widgets import Input, ListView, RichLog


def _load_tui_module():
    scripts_dir = Path(__file__).parents[1] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "llmd_event_tui",
        scripts_dir / "llmd_event_tui.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load llmd_event_tui.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tui = _load_tui_module()


class LlmdEventTuiTests(unittest.TestCase):
    def test_builtin_prompt_options_include_tool_probe(self) -> None:
        options = tui.prompt_options()
        tool_prompt = tui.builtin_prompt_by_key("tool_call")

        self.assertIn(("Tool call probe", "tool_call"), options)
        self.assertIsNotNone(tool_prompt)
        self.assertTrue(tool_prompt.use_debug_tool)

    def test_event_list_label_includes_receive_order_seq_and_type(self) -> None:
        event = InferenceGenerationStarted(
            generation_id="gen-1",
            seq=1,
            run_id="run-1",
            model="test-model",
        )

        label = tui.event_list_label(event, 7)

        self.assertEqual(label, "007 seq=1   inference.generation.started")

    def test_event_detail_text_renders_json_fields(self) -> None:
        event = InferenceContentDelta(
            generation_id="gen-1",
            seq=2,
            run_id="run-1",
            delta="hello",
        )

        detail = tui.event_detail_text(event)

        self.assertIn('"type": "inference.content.delta"', detail)
        self.assertIn('"delta": "hello"', detail)

    def test_app_initializes_from_args(self) -> None:
        args = tui.parse_args(["custom prompt", "--with-debug-tool"])
        app = tui.LlmdEventTui(args)

        self.assertEqual(app.args.prompt, "custom prompt")
        self.assertTrue(app.args.with_debug_tool)

    def test_app_mounts_prompt_input_event_list_and_detail_pane(self) -> None:
        async def run() -> None:
            app = tui.LlmdEventTui(tui.parse_args([]))
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause()
                self.assertTrue(app.query_one("#prompt-input", Input).value)
                self.assertIsInstance(app.query_one("#event-list"), ListView)
                self.assertIsInstance(app.query_one("#detail"), RichLog)

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
