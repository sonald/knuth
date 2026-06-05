import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.messages import ToolCall
from knuth_runtime.approval import MemoryApprovalService
from knuth_runtime.policy import PolicyEngine
from knuth_toold import ToolBroker, ToolIntent, ToolProposalStatus, create_default_registry


class DefaultToolRegistryTests(unittest.TestCase):
    def test_default_registry_exposes_required_tools(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            registry = create_default_registry(Path(workspace))

            names = {spec.name for spec in registry.specs()}

            self.assertEqual(
                names, {"read_file", "write_file", "shell", "python", "knuth.ask_user"}
            )

    def test_file_tools_write_and_read_workspace_file(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            registry = create_default_registry(Path(workspace))

            write_result = anyio.run(
                registry.execute,
                ToolCall(
                    name="write_file",
                    arguments={"path": "notes/hello.txt", "content": "hello knuth"},
                ),
            )
            read_result = anyio.run(
                registry.execute,
                ToolCall(name="read_file", arguments={"path": "notes/hello.txt"}),
            )

            self.assertTrue(write_result.ok)
            self.assertEqual(read_result.content, "hello knuth")

    def test_process_tools_capture_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            registry = create_default_registry(Path(workspace))

            shell_result = anyio.run(
                registry.execute,
                ToolCall(name="shell", arguments={"command": "printf shell-ok"}),
            )
            python_result = anyio.run(
                registry.execute,
                ToolCall(name="python", arguments={"code": "print('python-ok')"}),
            )

            self.assertTrue(shell_result.ok)
            self.assertEqual(shell_result.content, "shell-ok")
            self.assertTrue(python_result.ok)
            self.assertEqual(python_result.content.strip(), "python-ok")

    def test_tool_broker_uses_policy_for_approval_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            approvals = MemoryApprovalService()
            registry = create_default_registry(Path(workspace))
            broker = ToolBroker(registry, PolicyEngine(approvals))

            read = anyio.run(
                broker.propose,
                "run-1",
                ToolIntent(id="call-read", name="read_file", arguments={"path": "x"}),
            )
            write = anyio.run(
                broker.propose,
                "run-1",
                ToolIntent(
                    id="call-write",
                    name="write_file",
                    arguments={"path": "x", "content": "y"},
                ),
            )

            self.assertEqual(read.status, ToolProposalStatus.ALLOWED)
            self.assertEqual(write.status, ToolProposalStatus.REQUIRES_APPROVAL)
            self.assertIsNotNone(write.approval)

    def test_tool_execution_record_converts_result_to_tool_message(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            registry = create_default_registry(Path(workspace))
            broker = ToolBroker(registry)
            proposal = anyio.run(
                broker.propose,
                "run-1",
                ToolIntent(
                    id="call-1",
                    name="write_file",
                    arguments={"path": "x.txt", "content": "hello"},
                ),
            )
            record = anyio.run(broker.execute, "run-1", proposal)
            message = record.to_tool_result_message()

            self.assertEqual(message.tool_call_id, "call-1")
            self.assertEqual(message.tool_name, "write_file")

    def test_tool_broker_denies_invalid_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            registry = create_default_registry(Path(workspace))
            broker = ToolBroker(registry)
            proposal = anyio.run(
                broker.propose,
                "run-1",
                ToolIntent(id="call-1", name="read_file", arguments={}),
            )

            self.assertEqual(proposal.status, ToolProposalStatus.DENIED)
            self.assertEqual(proposal.error.code, "invalid_tool_arguments")


if __name__ == "__main__":
    unittest.main()
