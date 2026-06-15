import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.invocations import ToolInvocation, args_hash_for
from knuth.core.tools import ToolResult
from knuth_cli.tools.files import EditFileTool
from knuth_toold.builtins import ReadFileTool
from knuth_toold import ToolBroker, ToolRegistry
from knuth_toold.base import ToolManifest, ToolRuntimeContext


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


class _ToolSetProvider:
    name = "test"

    def __init__(self, *tools) -> None:
        self._tools = {tool.manifest.name: tool for tool in tools}

    async def list_tools(self) -> list[ToolManifest]:
        return [tool.manifest for tool in self._tools.values()]

    async def call_tool(
        self, invocation: ToolInvocation, ctx: ToolRuntimeContext
    ) -> ToolResult:
        return await self._tools[invocation.tool_name].invoke(invocation, ctx)


class CliReadFileToolTests(unittest.TestCase):
    def test_read_file_returns_numbered_slice(self) -> None:
        async def scenario(tmp_path: Path):
            file_path = tmp_path / "notes.txt"
            file_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            tool = ReadFileTool()

            result = await tool.invoke(
                _invocation(
                    "read_file", {"path": str(file_path), "offset": 2, "limit": 2}
                ),
                ToolRuntimeContext(run_id="run-1", tool_call_id="call-1"),
            )

            return result

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir))

        self.assertTrue(result.ok)
        self.assertIn(
            f"File({Path(temp_dir) / 'notes.txt'}) - Lines 2-3 of 3 total:",
            result.content,
        )
        self.assertIn("   2: beta", result.content)
        self.assertIn("   3: gamma", result.content)

    def test_read_file_rejects_requests_over_32kib_without_partial_content(self) -> None:
        async def scenario(tmp_path: Path):
            file_path = tmp_path / "big.txt"
            file_path.write_text(("a" * 20000) + "\n" + ("b" * 20000) + "\n")
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(ReadFileTool()))
            await registry.refresh()
            broker = ToolBroker(registry)

            return await broker.execute(
                _invocation(
                    "read_file", {"path": str(file_path), "offset": 1, "limit": 2}
                )
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir))

        self.assertFalse(result.ok)
        self.assertEqual(result.content, "")
        self.assertIn("exceeds read_file max of 32768 bytes", result.error.message)
        self.assertIn("no content returned", result.error.message)


class CliEditFileToolTests(unittest.TestCase):
    def test_edit_file_replaces_unique_match(self) -> None:
        async def scenario(tmp_path: Path):
            file_path = tmp_path / "notes.txt"
            file_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            tool = EditFileTool()

            result = await tool.invoke(
                _invocation(
                    "edit_file",
                    {
                        "path": str(file_path),
                        "old_string": "beta",
                        "new_string": "BETA",
                    },
                ),
                ToolRuntimeContext(run_id="run-1", tool_call_id="call-1"),
            )

            return result, file_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            result, content = anyio.run(scenario, Path(temp_dir))

        self.assertTrue(result.ok)
        self.assertEqual(content, "alpha\nBETA\ngamma\n")
        self.assertIn(f"Edited {Path(temp_dir) / 'notes.txt'}", result.content)
        self.assertIn("replacements=1", result.content)
        self.assertIn("encoding=utf-8", result.content)

    def test_edit_file_requires_unique_match_unless_replace_all(self) -> None:
        async def scenario(tmp_path: Path):
            file_path = tmp_path / "notes.txt"
            file_path.write_text("beta\nbeta\n", encoding="utf-8")
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(EditFileTool()))
            await registry.refresh()
            broker = ToolBroker(registry)

            failed = await broker.execute(
                _invocation(
                    "edit_file",
                    {
                        "path": str(file_path),
                        "old_string": "beta",
                        "new_string": "BETA",
                    },
                )
            )
            succeeded = await broker.execute(
                _invocation(
                    "edit_file",
                    {
                        "path": str(file_path),
                        "old_string": "beta",
                        "new_string": "BETA",
                        "replace_all": True,
                    },
                )
            )
            return failed, succeeded, file_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            failed, succeeded, content = anyio.run(scenario, Path(temp_dir))

        self.assertFalse(failed.ok)
        self.assertIn("found 2 matches", failed.error.message)
        self.assertIn("replace_all", failed.error.message)
        self.assertTrue(succeeded.ok)
        self.assertEqual(content, "BETA\nBETA\n")

    def test_edit_file_preserves_utf16_encoding(self) -> None:
        async def scenario(tmp_path: Path):
            file_path = tmp_path / "notes.txt"
            file_path.write_text("alpha\nbeta\n", encoding="utf-16")
            tool = EditFileTool()

            result = await tool.invoke(
                _invocation(
                    "edit_file",
                    {
                        "path": str(file_path),
                        "old_string": "beta",
                        "new_string": "BETA",
                    },
                ),
                ToolRuntimeContext(run_id="run-1", tool_call_id="call-1"),
            )

            return result, file_path.read_text(encoding="utf-16")

        with tempfile.TemporaryDirectory() as temp_dir:
            result, content = anyio.run(scenario, Path(temp_dir))

        self.assertTrue(result.ok)
        self.assertEqual(content, "alpha\nBETA\n")
        self.assertIn("encoding=utf-16", result.content)


if __name__ == "__main__":
    unittest.main()
