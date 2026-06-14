from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from knuth_im import build_runtime


def _tool_names(tools: list[dict]) -> set[str]:
    return {
        tool.get("function", {}).get("name")
        for tool in tools
        if isinstance(tool, dict)
    }


class KnuthIMRuntimeFactoryTests(unittest.TestCase):
    def test_build_runtime_exposes_cli_tools(self) -> None:
        env = {
            "KNUTH_API_KEY": "test-key",
            "KNUTH_BASE_URL": "https://example.invalid/v1",
            "KNUTH_MODEL": "test-model",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, env):
                runtime = build_runtime(db_path=Path(temp_dir, "knuth-im.db"))

                async def scenario():
                    return await runtime.tools()

                names = _tool_names(anyio.run(scenario))

        self.assertIn("read_file", names)
        self.assertIn("glob", names)
        self.assertIn("grep", names)
        self.assertIn("shell", names)


if __name__ == "__main__":
    unittest.main()
