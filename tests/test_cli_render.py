import io
import unittest
from types import SimpleNamespace

import anyio
from rich.console import Console

from knuth_cli.render import EventRenderer
from knuth_toold.process_output import render_tagged_process_output


class EventRendererShellToolTests(unittest.TestCase):
    def test_tool_started_can_use_seeded_tool_name_after_resume(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        renderer = EventRenderer(console)
        renderer.remember_tool_names({"call_write": "write_file"})

        anyio.run(
            renderer.handle_event,
            SimpleNamespace(
                type="tool.invocation_started",
                tool_call_id="call_write",
            ),
        )

        self.assertIn("write_file", output.getvalue())
        self.assertNotIn("call_write", output.getvalue())

    def test_shell_tool_completion_renders_structured_output(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        renderer = EventRenderer(console)
        observation = render_tagged_process_output(
            stdout="hello\n",
            stderr="warn\n",
            return_code=0,
            offload={"status": "inline"},
        )

        anyio.run(
            renderer.handle_event,
            SimpleNamespace(
                type="tool.invocation_completed",
                tool_name="shell",
                outcome="succeeded",
                observation=observation,
            ),
        )

        rendered = output.getvalue()
        self.assertIn("✔ shell exit 0", rendered)
        self.assertIn("stdout:", rendered)
        self.assertIn("hello", rendered)
        self.assertIn("stderr:", rendered)
        self.assertIn("warn", rendered)

    def test_shell_tool_completion_renders_offload_paths(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        renderer = EventRenderer(console)
        observation = render_tagged_process_output(
            stdout="abcde",
            stderr="",
            return_code=0,
            offload={
                "status": "offloaded",
                "stdout": {"id": "art_stdout", "path": "/tmp/stdout.txt"},
                "stderr": {"id": "art_stderr", "path": "/tmp/stderr.txt"},
            },
        )

        anyio.run(
            renderer.handle_event,
            SimpleNamespace(
                type="tool.invocation_completed",
                tool_name="shell",
                outcome="succeeded",
                observation=observation,
            ),
        )

        rendered = output.getvalue()
        self.assertIn("offload:", rendered)
        self.assertIn("/tmp/stdout.txt", rendered)
        self.assertIn("/tmp/stderr.txt", rendered)
        self.assertIn("art_stdout", rendered)
        self.assertIn("art_stderr", rendered)

    def test_shell_tool_completion_falls_back_on_unparseable_observation(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        renderer = EventRenderer(console)

        anyio.run(
            renderer.handle_event,
            SimpleNamespace(
                type="tool.invocation_completed",
                tool_name="shell",
                outcome="succeeded",
                observation="plain output",
            ),
        )

        self.assertIn("✔ shell — plain output", output.getvalue())

    def test_non_shell_tool_completion_renders_first_observation_lines(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        renderer = EventRenderer(console)

        anyio.run(
            renderer.handle_event,
            SimpleNamespace(
                type="tool.invocation_completed",
                tool_name="read_file",
                outcome="succeeded",
                observation="\n".join(
                    [
                        "File(notes.txt) - Lines 1-8 of 8 total:",
                        "   1: alpha",
                        "   2: beta",
                        "   3: gamma",
                        "   4: delta",
                        "   5: epsilon",
                        "   6: zeta",
                        "   7: eta",
                        "   8: theta",
                    ]
                ),
            ),
        )

        rendered = output.getvalue()
        self.assertIn("✔ read_file", rendered)
        self.assertIn("File(notes.txt)", rendered)
        self.assertIn("1: alpha", rendered)
        self.assertIn("5: epsilon", rendered)
        self.assertIn("… 3 more lines", rendered)
        self.assertNotIn("6: zeta", rendered)
        self.assertNotIn("8: theta", rendered)


if __name__ == "__main__":
    unittest.main()
