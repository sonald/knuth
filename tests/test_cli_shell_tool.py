import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.invocations import ToolInvocation, args_hash_for
from knuth.core.tools import ToolExecutionOutcome, ToolResult
from knuth_runtime import FilesystemArtifactStore, RegexSecretRedactor
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
        async def scenario():
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(ShellTool()))
            await registry.refresh()
            broker = ToolBroker(registry)

            return await broker.execute(
                _invocation({"command": "printf 'hello<&>\\n'; printf 'warn\\n' >&2"})
            )

        result = anyio.run(scenario)

        parsed = parse_tagged_process_output(result.result.content or "")
        self.assertEqual(result.outcome, ToolExecutionOutcome.SUCCEEDED)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.stdout, "hello<&>\n")
        self.assertEqual(parsed.stderr, "warn\n")
        self.assertEqual(parsed.return_code, 0)
        self.assertEqual(parsed.offload["status"], "inline")

    def test_shell_offloads_large_output_with_metadata(self) -> None:
        async def scenario(artifact_root: Path):
            tool = ShellTool(
                threshold_bytes=12,
                preview_bytes=5,
            )
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(tool))
            await registry.refresh()
            store = FilesystemArtifactStore(
                artifact_root,
                redactor=RegexSecretRedactor(),
            )
            broker = ToolBroker(registry, artifact_sink_provider=store)

            return await broker.execute(
                _invocation(
                    {"command": "printf 'abcdefghijklmnop'; printf 'qrstuvwxyz' >&2"},
                    run_id="run-big",
                )
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir) / "artifacts")
            parsed = parse_tagged_process_output(result.result.content or "")
            self.assertIsNotNone(parsed)
            offload = parsed.offload
            stdout_path = Path(offload["stdout"]["path"])
            stderr_path = Path(offload["stderr"]["path"])
            stdout_content = stdout_path.read_text(encoding="utf-8")
            stderr_content = stderr_path.read_text(encoding="utf-8")

        self.assertEqual(result.outcome, ToolExecutionOutcome.SUCCEEDED)
        self.assertEqual(parsed.stdout, "abcde")
        self.assertEqual(parsed.stderr, "qrstu")
        self.assertEqual(parsed.return_code, 0)
        self.assertEqual(offload["status"], "offloaded")
        self.assertEqual(result.result.artifacts, [
            offload["stdout"]["id"],
            offload["stderr"]["id"],
        ])
        self.assertTrue(result.result.condensed)
        self.assertEqual(stdout_content, "abcdefghijklmnop")
        self.assertEqual(stderr_content, "qrstuvwxyz")
        self.assertEqual(offload["stdout"]["bytes"], 16)
        self.assertEqual(offload["stderr"]["bytes"], 10)

    def test_shell_large_output_without_artifact_sink_is_inline_l2_fallback(self) -> None:
        async def scenario():
            registry = ToolRegistry()
            registry.add_provider(
                _ToolSetProvider(ShellTool(threshold_bytes=12, preview_bytes=5))
            )
            await registry.refresh()
            broker = ToolBroker(registry)
            return await broker.execute(
                _invocation({"command": "printf 'abcdefghijklmnop'"})
            )

        result = anyio.run(scenario)
        parsed = parse_tagged_process_output(result.result.content or "")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.stdout, "abcdefghijklmnop")
        self.assertEqual(parsed.offload["status"], "inline")
        self.assertEqual(parsed.offload["reason"], "artifact_sink_unavailable")
        self.assertEqual(result.result.artifacts, [])
        self.assertFalse(result.result.condensed)

    def test_shell_nonzero_exit_keeps_structured_output(self) -> None:
        async def scenario():
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(ShellTool()))
            await registry.refresh()
            broker = ToolBroker(registry)

            return await broker.execute(
                _invocation({"command": "printf 'nope' >&2; exit 7"})
            )

        result = anyio.run(scenario)

        parsed = parse_tagged_process_output(result.result.content or "")
        self.assertEqual(result.outcome, ToolExecutionOutcome.FAILED)
        self.assertEqual(result.result.error.code, "process_failed")
        self.assertTrue(result.result.error.retryable)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.stderr, "nope")
        self.assertEqual(parsed.return_code, 7)

    def test_unsafe_tool_call_id_is_structured_failure(self) -> None:
        # A tool_call_id with a path separator must not crash execute() out of
        # band; the sink failure becomes a structured tool failure.
        async def scenario(artifact_root: Path):
            registry = ToolRegistry()
            registry.add_provider(_ToolSetProvider(ShellTool()))
            await registry.refresh()
            store = FilesystemArtifactStore(
                artifact_root, redactor=RegexSecretRedactor()
            )
            broker = ToolBroker(registry, artifact_sink_provider=store)
            args = {"command": "echo hi"}
            invocation = ToolInvocation(
                tool_call_id="bad/id",
                run_id="run-1",
                batch_id="batch-1",
                step_id="step-1",
                tool_name="shell",
                args=args,
                args_hash=args_hash_for(args),
            )
            return await broker.execute(invocation)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = anyio.run(scenario, Path(temp_dir) / "artifacts")
        self.assertEqual(result.outcome, ToolExecutionOutcome.FAILED)
        self.assertEqual(result.result.error.code, "artifact_sink_unavailable")


if __name__ == "__main__":
    unittest.main()
