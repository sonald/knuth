from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from knuth_runtime.skills import SkillReminderMiddleware
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
            with patch.dict(os.environ, env, clear=True):
                runtime = build_runtime(db_path=Path(temp_dir, "knuth-im.db"))

                async def scenario():
                    return await runtime.tools()

                names = _tool_names(anyio.run(scenario))

        self.assertIn("read_file", names)
        self.assertIn("glob", names)
        self.assertIn("grep", names)
        self.assertIn("shell", names)

    def test_build_runtime_uses_cli_skill_environment_config(self) -> None:
        env = {
            "KNUTH_API_KEY": "test-key",
            "KNUTH_BASE_URL": "https://example.invalid/v1",
            "KNUTH_MODEL": "test-model",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "im-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: im-skill",
                        "description: Skill visible to the IM host.",
                        "---",
                        "",
                        "Use this skill from knuth-im.",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {**env, "KNUTH_SKILL_ROOTS": str(root / "skills")},
                clear=True,
            ):
                runtime = build_runtime(db_path=root / "knuth-im.db")

            middlewares = runtime._services.message_middleware_runner.middlewares
            reminder = next(
                item
                for item in middlewares
                if isinstance(item, SkillReminderMiddleware)
            )
            names = [
                skill.info.metadata.name
                for skill in reminder._manager.list_skills()
            ]

        self.assertEqual(names, ["im-skill"])


if __name__ == "__main__":
    unittest.main()
