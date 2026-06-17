"""Build the knuth-im ``AgentRuntime`` from host-level configuration."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from knuth_cli.prompts import build_cli_message_middlewares, build_cli_system_sections
from knuth_cli.tools import create_cli_tool_provider
from knuth_llmd import InferenceConfig, LiteLLMInferenceClient
from knuth_runtime import AgentRuntime, build_sqlite_runtime
from knuth_toold import ToolProvider


def load_dotenv(path: str | Path = ".env") -> None:
    """Small repo-local ``.env`` loader; existing process environment wins."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def build_runtime(
    db_path: str | Path | None = None,
    *,
    tool_providers: Iterable[ToolProvider] = (),
) -> AgentRuntime:
    api_key = os.environ.get("KNUTH_API_KEY")
    base_url = os.environ.get("KNUTH_BASE_URL")
    model = os.environ.get("KNUTH_MODEL")
    if not (api_key and base_url and model):
        raise ValueError(
            "Set KNUTH_API_KEY, KNUTH_BASE_URL, and KNUTH_MODEL "
            "(or put them in .env)"
        )
    timeout = float(os.environ.get("KNUTH_TIMEOUT") or 60.0)
    system_prompt = os.environ.get("KNUTH_SYSTEM_PROMPT")
    return build_sqlite_runtime(
        inference_client=LiteLLMInferenceClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        ),
        inference_config=InferenceConfig(timeout_s=timeout),
        db_path=db_path or Path("~/.knuth/knuth-im.db"),
        section_providers=build_cli_system_sections(system_prompt),
        message_middlewares=build_cli_message_middlewares(),
        tool_providers=[create_cli_tool_provider(), *tool_providers],
        include_default_tools=True,
    )
