from pathlib import Path

from knuth_cli.tools.files import EditFileTool, ReadFileTool, WriteFileTool
from knuth_cli.tools.shell import ShellTool


def create_cli_tools(cwd: Path | str | None = None):
    return [
        ReadFileTool(cwd),
        WriteFileTool(cwd),
        EditFileTool(cwd),
        ShellTool(cwd),
    ]


__all__ = ["EditFileTool", "ReadFileTool", "ShellTool", "WriteFileTool", "create_cli_tools"]
