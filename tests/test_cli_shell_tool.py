import json
import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.invocations import ToolInvocation, args_hash_for
from knuth.core.tools import ToolResult
from knuth_toold.builtins import ShellTool
from knuth_toold.process_output import parse_tagged_process_output
from knuth_toold import ToolBroker, ToolRegistry
from knuth_toold.base import ToolManifest, ToolRuntimeContext


def _invocation(args: dict, *, run_id: str = "run-1") -> ToolInvocation:
    return ToolInvocation(
        tool_call_id="call-1",
        run_id=run_id,
        batch_id="batch-1",
        step_id="step-1",
        tool_name="shell",
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


class CliShellToolTests(unittest.TestCase):
    def test_shell_returns_structured_output(self) -> None:
        async def scenario(tmp_path: Path, offload_root: Path):
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(ShellTool(offload_root=offload_root)))
            await registry.refresh()
            broker = ToolBroker(registry)

            return await broker.execute(
                _invocation({"command": "printf 'hello<&>\\n'; printf 'warn\\n' >&2"})
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            result = anyio.run(scenario, tmp_path, tmp_path / "offload")

        parsed = parse_tagged_process_output(result.content or "")
        self.assertTrue(result.ok)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.stdout, "hello<&>\n")
        self.assertEqual(parsed.stderr, "warn\n")
        self.assertEqual(parsed.return_code, 0)
        self.assertEqual(parsed.offload["status"], "inline")

    def test_shell_offloads_large_output_with_metadata(self) -> None:
        async def scenario(tmp_path: Path, offload_root: Path):
            tool = ShellTool(
                offload_root=offload_root,
                threshold_bytes=12,
                preview_bytes=5,
            )
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(tool))
            await registry.refresh()
            broker = ToolBroker(registry)

            return await broker.execute(
                _invocation(
                    {"command": "printf 'abcdefghijklmnop'; printf 'qrstuvwxyz' >&2"},
                    run_id="run-big",
                )
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            result = anyio.run(scenario, tmp_path, tmp_path / "offload")
            parsed = parse_tagged_process_output(result.content or "")
            self.assertIsNotNone(parsed)
            offload = parsed.offload
            stdout_path = Path(offload["stdout"]["path"])
            stderr_path = Path(offload["stderr"]["path"])
            result_path = Path(offload["result_path"])
            metadata = json.loads(result_path.read_text(encoding="utf-8"))
            stdout_content = stdout_path.read_text(encoding="utf-8")
            stderr_content = stderr_path.read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        self.assertEqual(parsed.stdout, "abcde")
        self.assertEqual(parsed.stderr, "qrstu")
        self.assertEqual(parsed.return_code, 0)
        self.assertEqual(offload["status"], "offloaded")
        self.assertEqual(stdout_content, "abcdefghijklmnop")
        self.assertEqual(stderr_content, "qrstuvwxyz")
        self.assertNotIn("command", metadata)
        self.assertEqual(metadata["run_id"], "run-big")
        self.assertEqual(metadata["tool_call_id"], "call-1")
        self.assertEqual(metadata["stdout"]["bytes"], 16)
        self.assertEqual(metadata["stderr"]["bytes"], 10)
        self.assertIn("command_sha256", metadata)

    def test_shell_nonzero_exit_keeps_structured_output(self) -> None:
        async def scenario(tmp_path: Path, offload_root: Path):
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(ShellTool(offload_root=offload_root)))
            await registry.refresh()
            broker = ToolBroker(registry)

            return await broker.execute(
                _invocation({"command": "printf 'nope' >&2; exit 7"})
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            result = anyio.run(scenario, tmp_path, tmp_path / "offload")

        parsed = parse_tagged_process_output(result.content or "")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "process_failed")
        self.assertTrue(result.error.retryable)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.stderr, "nope")
        self.assertEqual(parsed.return_code, 7)


if __name__ == "__main__":
    unittest.main()
