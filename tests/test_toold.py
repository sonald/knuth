import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.invocations import (
    ToolCallDecision,
    ToolEffect,
    ToolInvocation,
    ToolRisk,
    args_hash_for,
)
from knuth.core.tools import ToolResult
from knuth_runtime.policy import PolicyEngine
from knuth_toold import (
    ToolBroker,
    ToolManifest,
    ToolRuntimeContext,
    ToolRegistry,
    create_default_registry,
)


def _invocation(name: str, args: dict, tool_call_id: str = "call-1") -> ToolInvocation:
    return ToolInvocation(
        tool_call_id=tool_call_id,
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


class DefaultToolRegistryTests(unittest.TestCase):
    def test_default_registry_exposes_required_tools(self) -> None:
        registry = create_default_registry()
        broker = ToolBroker(registry)

        tools = anyio.run(broker.list_visible_tools, "run-1")
        names = {tool["function"]["name"] for tool in tools}

        self.assertEqual(names, {"read_file", "write_file", "shell", "python"})

    def test_entry_point_discovery_is_off_by_default(self) -> None:
        registry = ToolRegistry()
        self.assertFalse(registry._enable_entry_point_discovery)

    def test_tool_name_conflicts_are_rejected(self) -> None:
        registry = create_default_registry()
        registry.add_provider(_ToolSetProvider(_SleepyTool(name="read_file")))

        with self.assertRaisesRegex(ValueError, "Tool name conflict: read_file"):
            anyio.run(registry.refresh)

    def test_file_tools_write_and_read_workspace_file(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            registry = create_default_registry()
            broker = ToolBroker(registry)
            anyio.run(registry.refresh)

            file_path = str(Path(workspace, "notes", "hello.txt"))
            write_result = anyio.run(
                broker.execute,
                _invocation(
                    "write_file",
                    {"path": file_path, "content": "hello knuth"},
                ),
            )
            read_result = anyio.run(
                broker.execute,
                _invocation("read_file", {"path": file_path}),
            )

            self.assertTrue(write_result.ok)
            self.assertEqual(read_result.content, "hello knuth")

    def test_process_tools_capture_stdout(self) -> None:
        registry = create_default_registry()
        broker = ToolBroker(registry)
        anyio.run(registry.refresh)

        shell_result = anyio.run(
            broker.execute,
            _invocation("shell", {"command": "printf shell-ok"}),
        )
        python_result = anyio.run(
            broker.execute,
            _invocation("python", {"code": "print('python-ok')"}),
        )

        self.assertTrue(shell_result.ok)
        self.assertEqual(shell_result.content, "shell-ok")
        self.assertTrue(python_result.ok)
        self.assertEqual(python_result.content.strip(), "python-ok")

    def test_tool_broker_uses_policy_for_approval_decisions(self) -> None:
        registry = create_default_registry()
        broker = ToolBroker(registry, PolicyEngine())

        read = anyio.run(broker.propose, "run-1", "read_file", {"path": "x"})
        write = anyio.run(
            broker.propose, "run-1", "write_file", {"path": "x", "content": "y"}
        )

        self.assertEqual(read.decision, ToolCallDecision.ALLOWED)
        self.assertEqual(write.decision, ToolCallDecision.REQUIRES_APPROVAL)
        self.assertEqual(write.effect, ToolEffect.LOCAL_WRITE)
        self.assertTrue(write.approval_title)

    def test_propose_is_pure_and_repeatable(self) -> None:
        registry = create_default_registry()
        broker = ToolBroker(registry, PolicyEngine())

        first = anyio.run(
            broker.propose, "run-1", "write_file", {"path": "x", "content": "y"}
        )
        second = anyio.run(
            broker.propose, "run-1", "write_file", {"path": "x", "content": "y"}
        )

        self.assertEqual(first.decision, second.decision)

    def test_tool_broker_wraps_execution_errors_as_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            registry = create_default_registry()
            broker = ToolBroker(registry)
            anyio.run(registry.refresh)

            result = anyio.run(
                broker.execute,
                _invocation(
                    "read_file", {"path": str(Path(workspace, "missing.txt"))}
                ),
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error.code, "FileNotFoundError")

    def test_tool_broker_denies_invalid_arguments(self) -> None:
        registry = create_default_registry()
        broker = ToolBroker(registry)

        proposal = anyio.run(broker.propose, "run-1", "read_file", {})

        self.assertEqual(proposal.decision, ToolCallDecision.DENIED)
        self.assertEqual(proposal.error.code, "invalid_tool_arguments")

    def test_tool_broker_denies_unknown_tool(self) -> None:
        registry = create_default_registry()
        broker = ToolBroker(registry)

        proposal = anyio.run(broker.propose, "run-1", "nope", {})

        self.assertEqual(proposal.decision, ToolCallDecision.DENIED)
        self.assertEqual(proposal.error.code, "tool_not_found")


class _SleepyTool:
    def __init__(self, name: str = "sleepy") -> None:
        self._name = name

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name=self._name,
            description="Sleeps longer than its timeout.",
            parameters={"type": "object", "properties": {}},
            timeout_s=0.05,
        )

    async def invoke(self, invocation, ctx) -> ToolResult:
        await anyio.sleep(5)
        return ToolResult.success(content="never")


class TimeoutTests(unittest.TestCase):
    def test_execute_enforces_manifest_timeout(self) -> None:
        registry = ToolRegistry()
        registry.add_provider(_ToolSetProvider(_SleepyTool()))
        broker = ToolBroker(registry)
        anyio.run(registry.refresh)

        result = anyio.run(broker.execute, _invocation("sleepy", {}))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "tool_timeout")
        self.assertTrue(result.error.retryable)


if __name__ == "__main__":
    unittest.main()
