import os
import tempfile
import unittest
from pathlib import Path

import anyio

from knuth.core.invocations import ToolInvocation, args_hash_for
from knuth.core.skills import SkillSource
from knuth.core.tools import ToolResultStatus
from knuth_toold import ToolRuntimeContext
from knuth_toold.skills import (
    SkillHotReloadService,
    SkillManager,
    SkillRoot,
    SkillToolProvider,
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


class SkillToolProviderTests(unittest.TestCase):
    def test_skill_tool_loads_skill_content_from_configured_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "example-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: example-skill",
                        "description: Use when an example skill is needed.",
                        "allowed-tools: shell read_file",
                        "unexpected-key: kept as warning",
                        "---",
                        "",
                        "# Example Skill",
                        "",
                        "Follow the example workflow.",
                    ]
                ),
                encoding="utf-8",
            )
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )
            provider = SkillToolProvider(manager)

            async def scenario():
                manifests = await provider.list_tools()
                result = await provider.call_tool(
                    _invocation(
                        "skill",
                        {
                            "skill_name": "example-skill",
                            "args": "topic=demo",
                        },
                    ),
                    ToolRuntimeContext(run_id="run-1", tool_call_id="call-1"),
                )
                return manifests, result

            manifests, result = anyio.run(scenario)

            self.assertEqual([manifest.name for manifest in manifests], ["skill"])
            self.assertEqual(result.status, ToolResultStatus.SUCCESS)
            self.assertIn("Skill 'example-skill' loaded successfully.", result.content)
            self.assertIn(f"Base directory for this skill: {skill_dir}", result.content)
            self.assertIn("Follow the example workflow.", result.content)
            self.assertIn("Skill arguments: topic=demo", result.content)
            infos = manager.to_skill_infos()
            self.assertEqual(infos[0].metadata.name, "example-skill")
            self.assertEqual(infos[0].metadata.allowed_tools, ["shell", "read_file"])
            self.assertEqual(
                manager.list_skills()[0].validation_warnings,
                ["unknown frontmatter key: unexpected-key"],
            )

    def test_manager_uses_root_priority_and_light_catalog_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            high = Path(temp_dir, "project")
            low = Path(temp_dir, "user")
            high_skill = high / "shared-skill"
            low_skill = low / "shared-skill"
            high_skill.mkdir(parents=True)
            low_skill.mkdir(parents=True)
            high_file = high_skill / "SKILL.md"
            high_file.write_text(
                "\n".join(
                    [
                        "---",
                        "name: shared-skill",
                        "description: Project version.",
                        "---",
                        "",
                        "Project body v1.",
                    ]
                ),
                encoding="utf-8",
            )
            low_skill.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: shared-skill",
                        "description: User version.",
                        "---",
                        "",
                        "User body.",
                    ]
                ),
                encoding="utf-8",
            )
            manager = SkillManager(
                [
                    SkillRoot(source=SkillSource.PROJECT, path=str(high)),
                    SkillRoot(source=SkillSource.USER, path=str(low)),
                ]
            )

            first = manager.refresh_if_dirty()
            high_file.write_text(
                "\n".join(
                    [
                        "---",
                        "name: shared-skill",
                        "description: Project version.",
                        "---",
                        "",
                        "Project body v2.",
                    ]
                ),
                encoding="utf-8",
            )
            manager.invalidate("body changed")
            second = manager.refresh_if_dirty()

            self.assertEqual(len(second.skills), 1)
            self.assertEqual(second.skills[0].info.source, SkillSource.PROJECT)
            self.assertEqual(second.skills[0].content, "Project body v2.")
            self.assertEqual(second.version, first.version + 1)
            self.assertEqual(second.catalog_digest, first.catalog_digest)

    def test_hot_reload_service_invalidates_manager_for_safe_point_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "example-skill"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "\n".join(
                    [
                        "---",
                        "name: example-skill",
                        "description: Use v1.",
                        "---",
                        "",
                        "Body v1.",
                    ]
                ),
                encoding="utf-8",
            )
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )
            first = manager.refresh_if_dirty()
            service = SkillHotReloadService(
                manager,
                poll_interval_s=0.01,
                debounce_ms=1,
            )

            async def scenario():
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(service.run)
                    await anyio.sleep(0.03)
                    skill_file.write_text(
                        "\n".join(
                            [
                                "---",
                                "name: example-skill",
                                "description: Use v1.",
                                "---",
                                "",
                                "Body v2.",
                            ]
                        ),
                        encoding="utf-8",
                    )
                    with anyio.fail_after(1):
                        while True:
                            if manager.is_dirty:
                                stale = manager.current_snapshot()
                                self.assertEqual(stale.version, first.version)
                                self.assertEqual(stale.skills[0].content, "Body v1.")
                                snapshot = manager.refresh_if_dirty()
                                task_group.cancel_scope.cancel()
                                return snapshot
                            await anyio.sleep(0.02)

            second = anyio.run(scenario)

            self.assertEqual(second.catalog_digest, first.catalog_digest)
            self.assertEqual(second.version, first.version + 1)
            self.assertEqual(second.skills[0].content, "Body v2.")

    def test_hot_reload_service_detects_pending_root_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "missing", "skills")
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )
            first = manager.refresh_if_dirty()
            service = SkillHotReloadService(
                manager,
                poll_interval_s=0.01,
                debounce_ms=1,
            )

            async def scenario():
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(service.run)
                    await anyio.sleep(0.03)
                    skill_dir = root / "new-skill"
                    skill_dir.mkdir(parents=True)
                    skill_dir.joinpath("SKILL.md").write_text(
                        "\n".join(
                            [
                                "---",
                                "name: new-skill",
                                "description: New skill.",
                                "---",
                                "",
                                "New body.",
                            ]
                        ),
                        encoding="utf-8",
                    )
                    with anyio.fail_after(1):
                        while True:
                            snapshot = manager.refresh_if_dirty()
                            if (
                                snapshot.version > first.version
                                and [s.info.metadata.name for s in snapshot.skills]
                                == ["new-skill"]
                            ):
                                task_group.cancel_scope.cancel()
                                return snapshot
                            await anyio.sleep(0.02)

            second = anyio.run(scenario)

            self.assertNotEqual(second.catalog_digest, first.catalog_digest)

    def test_skill_tool_returns_error_when_content_exceeds_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            skill_dir = root / "large-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: large-skill",
                        "description: Use when a large skill is needed.",
                        "---",
                        "",
                        "0123456789abcdef",
                    ]
                ),
                encoding="utf-8",
            )
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )
            provider = SkillToolProvider(manager, max_observation_chars=8)

            result = anyio.run(
                provider.call_tool,
                _invocation("skill", {"skill_name": "large-skill"}),
                ToolRuntimeContext(run_id="run-1", tool_call_id="call-1"),
            )

            self.assertEqual(result.status, ToolResultStatus.ERROR)
            self.assertEqual(result.error.code, "skill_content_too_large")

    def test_invalid_skill_file_is_skipped_without_breaking_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            bad = root / "bad-skill"
            good = root / "good-skill"
            bad.mkdir(parents=True)
            good.mkdir(parents=True)
            bad.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: BadName",
                        "description: Invalid uppercase name.",
                        "---",
                        "",
                        "Bad body.",
                    ]
                ),
                encoding="utf-8",
            )
            good.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: good-skill",
                        "description: Valid skill.",
                        "---",
                        "",
                        "Good body.",
                    ]
                ),
                encoding="utf-8",
            )
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )

            snapshot = manager.refresh_if_dirty()

            self.assertEqual(
                [skill.info.metadata.name for skill in snapshot.skills],
                ["good-skill"],
            )

    def test_scan_follows_directory_symlinks_skips_git_and_avoids_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "skills")
            real = Path(temp_dir, "real")
            root.mkdir()
            real.mkdir()
            linked_skill = real / "linked-skill"
            linked_skill.mkdir()
            linked_skill.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: linked-skill",
                        "description: Linked skill.",
                        "---",
                        "",
                        "Linked body.",
                    ]
                ),
                encoding="utf-8",
            )
            git_skill = root / ".git" / "hidden-skill"
            git_skill.mkdir(parents=True)
            git_skill.joinpath("SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: hidden-skill",
                        "description: Hidden skill.",
                        "---",
                        "",
                        "Hidden body.",
                    ]
                ),
                encoding="utf-8",
            )
            try:
                os.symlink(linked_skill, root / "linked-skill")
                os.symlink(root, root / "cycle")
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            manager = SkillManager(
                [SkillRoot(source=SkillSource.PROJECT, path=str(root))]
            )

            snapshot = manager.refresh_if_dirty()

            self.assertEqual(
                [skill.info.metadata.name for skill in snapshot.skills],
                ["linked-skill"],
            )


if __name__ == "__main__":
    unittest.main()
