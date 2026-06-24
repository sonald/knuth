from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from knuth_llmd import InferenceConfig
from knuth_runtime import SkillRuntimeConfig, build_memory_runtime
from knuth.core.skills import SkillSource
from knuth_toold import SkillRoot
from knuth_agui import create_app
from knuth_im.__main__ import parse_server_config


class _NoopInferenceClient:
    model = "noop"

    async def stream(self, messages, tools, config, runtime=None):
        if False:
            yield None


class KnuthIMSidecarTests(unittest.TestCase):
    def _app(
        self,
        *,
        auth_token: str | None = None,
        skill_config: SkillRuntimeConfig | None = None,
    ):
        runtime = build_memory_runtime(
            inference_client=_NoopInferenceClient(),
            inference_config=InferenceConfig(),
            include_default_tools=False,
            skill_config=skill_config,
        )
        return create_app(runtime, auth_token=auth_token)

    def test_healthz_is_public_when_auth_token_is_configured(self) -> None:
        with TestClient(self._app(auth_token="secret")) as client:
            response = client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_protected_endpoint_requires_configured_token(self) -> None:
        with TestClient(self._app(auth_token="secret")) as client:
            missing = client.get("/threads")
            wrong = client.get("/threads", headers={"Authorization": "Bearer wrong"})
            allowed = client.get(
                "/threads", headers={"Authorization": "Bearer secret"}
            )
            alternate = client.get(
                "/threads", headers={"X-Knuth-Auth-Token": "secret"}
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(alternate.status_code, 200)

    def test_endpoints_remain_open_when_auth_token_is_not_configured(self) -> None:
        with TestClient(self._app()) as client:
            response = client.get("/threads")

        self.assertEqual(response.status_code, 200)

    def test_commands_endpoint_returns_builtin_and_skill_metadata_only(self) -> None:
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
                        "---",
                        "",
                        "Follow the example workflow.",
                    ]
                ),
                encoding="utf-8",
            )
            app = self._app(
                skill_config=SkillRuntimeConfig(
                    roots=[SkillRoot(source=SkillSource.PROJECT, path=str(root))],
                )
            )
            with TestClient(app) as client:
                response = client.get("/commands")

        self.assertEqual(response.status_code, 200)
        commands = response.json()["commands"]
        by_name = {command["name"]: command for command in commands}
        self.assertIn("usage", by_name)
        self.assertIn("skill:example-skill", by_name)
        self.assertEqual(by_name["skill:example-skill"]["source"], "skill")
        self.assertEqual(by_name["skill:example-skill"]["canonical"], "skill:example-skill")
        self.assertNotIn("handler", by_name["skill:example-skill"])
        self.assertNotIn("action", by_name["skill:example-skill"])

    def test_server_config_parses_flags_after_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            env_file = root / "sidecar.env"
            env_file.write_text(
                "\n".join(
                    [
                        "KNUTH_IM_HOST=127.0.0.9",
                        "KNUTH_IM_PORT=8123",
                        "KNUTH_IM_AUTH_TOKEN=env-token",
                    ]
                ),
                encoding="utf-8",
            )
            db_path = root / "sidecar.db"
            with patch.dict(os.environ, {}, clear=True):
                config = parse_server_config(
                    [
                        "--env-file",
                        str(env_file),
                        "--db-path",
                        str(db_path),
                        "--workspace",
                        str(workspace),
                        "--auth-token",
                        "flag-token",
                    ]
                )

        self.assertEqual(config.host, "127.0.0.9")
        self.assertEqual(config.port, 8123)
        self.assertEqual(config.db_path, db_path)
        self.assertEqual(config.workspace, workspace)
        self.assertEqual(config.auth_token, "flag-token")


if __name__ == "__main__":
    unittest.main()
