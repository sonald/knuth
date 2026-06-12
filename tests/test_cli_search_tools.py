import os
import shutil
import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.invocations import ToolInvocation, args_hash_for
from knuth_cli.tools.search import GlobTool, GrepTool
from knuth_toold.base import ToolRuntimeContext


def _invocation(name: str, args: dict) -> ToolInvocation:
    return ToolInvocation(
        tool_call_id="call-1",
        run_id="run-1",
        batch_id="batch-1",
        step_id="step-1",
        tool_name=name,
        args=args,
        args_hash=args_hash_for(args),
    )


def _ctx() -> ToolRuntimeContext:
    return ToolRuntimeContext(run_id="run-1", tool_call_id="call-1")


class CliGlobToolTests(unittest.TestCase):
    def test_glob_supports_braces_and_sorts_by_mtime(self) -> None:
        async def scenario(tmp_path: Path):
            old_file = tmp_path / "a.json"
            new_file = tmp_path / "b.yaml"
            third_file = tmp_path / "c.yaml"
            old_file.write_text("{}", encoding="utf-8")
            new_file.write_text("x: y", encoding="utf-8")
            third_file.write_text("z: q", encoding="utf-8")
            old_time = 1_700_000_000
            new_time = 1_700_000_100
            third_time = 1_700_000_200
            os.utime(old_file, (old_time, old_time))
            os.utime(new_file, (new_time, new_time))
            os.utime(third_file, (third_time, third_time))

            return await GlobTool().invoke(
                _invocation(
                    "glob",
                    {"pattern": str(tmp_path / "*.{json,yaml}"), "limit": 2},
                ),
                _ctx(),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir))

        self.assertTrue(result.ok)
        lines = result.content.splitlines()
        self.assertIn("matches=3", lines[0])
        self.assertIn("truncated=true", lines[0])
        self.assertTrue(lines[1].endswith("c.yaml"))
        self.assertTrue(lines[2].endswith("b.yaml"))


@unittest.skipIf(shutil.which("rg") is None, "ripgrep is required for grep tool tests")
class CliGrepToolTests(unittest.TestCase):
    def test_grep_defaults_to_files_with_matches(self) -> None:
        async def scenario(tmp_path: Path):
            first = tmp_path / "first.py"
            second = tmp_path / "second.txt"
            first.write_text("needle = 1\n", encoding="utf-8")
            second.write_text("needle\n", encoding="utf-8")

            return await GrepTool().invoke(
                _invocation(
                    "grep",
                    {"pattern": "needle", "path": str(tmp_path), "glob": "*.py"},
                ),
                _ctx(),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir))

        self.assertTrue(result.ok)
        self.assertIn("mode=files_with_matches", result.content)
        self.assertIn("first.py", result.content)
        self.assertNotIn("second.txt", result.content)

    def test_grep_content_returns_matching_lines_with_line_numbers(self) -> None:
        async def scenario(tmp_path: Path):
            file_path = tmp_path / "notes.txt"
            file_path.write_text("alpha\nneedle beta\n", encoding="utf-8")

            return await GrepTool().invoke(
                _invocation(
                    "grep",
                    {
                        "pattern": "needle",
                        "path": str(file_path),
                        "output_mode": "content",
                    },
                ),
                _ctx(),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir))

        self.assertTrue(result.ok)
        self.assertIn("notes.txt:2: needle beta", result.content)

    def test_grep_count_returns_match_count_per_file(self) -> None:
        async def scenario(tmp_path: Path):
            file_path = tmp_path / "notes.txt"
            file_path.write_text("needle\nother\nneedle\n", encoding="utf-8")

            return await GrepTool().invoke(
                _invocation(
                    "grep",
                    {
                        "pattern": "needle",
                        "path": str(tmp_path),
                        "output_mode": "count",
                    },
                ),
                _ctx(),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir))

        self.assertTrue(result.ok)
        self.assertIn("notes.txt: 2", result.content)


if __name__ == "__main__":
    unittest.main()
