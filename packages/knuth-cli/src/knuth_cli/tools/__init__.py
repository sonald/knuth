from knuth.core.invocations import ToolInvocation
from knuth.core.tools import ToolResult
from knuth_cli.tools.files import EditFileTool, ReadFileTool, WriteFileTool
from knuth_cli.tools.search import GlobTool, GrepTool
from knuth_cli.tools.shell import ShellTool
from knuth_toold.base import Tool, ToolManifest, ToolRuntimeContext
from knuth_toold.builtins import PythonTool


class CliToolProvider:
    name = "knuth-cli"

    def __init__(self) -> None:
        tools = (
            ReadFileTool(),
            WriteFileTool(),
            EditFileTool(),
            GlobTool(),
            GrepTool(),
            ShellTool(),
            PythonTool(),
        )
        self._tools: dict[str, Tool] = {tool.manifest.name: tool for tool in tools}

    async def list_tools(self) -> list[ToolManifest]:
        return [tool.manifest for tool in self._tools.values()]

    async def call_tool(
        self,
        invocation: ToolInvocation,
        ctx: ToolRuntimeContext,
    ) -> ToolResult:
        return await self._tools[invocation.tool_name].invoke(invocation, ctx)


def create_cli_tool_provider() -> CliToolProvider:
    return CliToolProvider()


__all__ = [
    "CliToolProvider",
    "EditFileTool",
    "GlobTool",
    "GrepTool",
    "ReadFileTool",
    "ShellTool",
    "WriteFileTool",
    "create_cli_tool_provider",
]
