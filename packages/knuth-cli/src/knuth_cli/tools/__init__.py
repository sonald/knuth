from knuth_cli.tools.files import EditFileTool, ReadFileTool, WriteFileTool
from knuth_cli.tools.shell import ShellTool


def create_cli_tools():
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ShellTool(),
    ]


__all__ = ["EditFileTool", "ReadFileTool", "ShellTool", "WriteFileTool", "create_cli_tools"]
