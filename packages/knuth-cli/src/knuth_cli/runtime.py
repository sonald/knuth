from __future__ import annotations

from pathlib import Path
from typing import Mapping

from knuth.core.messages import SystemSectionSource
from knuth_llmd import InferenceConfig, LiteLLMInferenceClient
from knuth_runtime import AgentRuntime, StaticSectionProvider, build_sqlite_runtime

from knuth_cli.config import load_config


KNUTH_CLI_BASE_IDENTITY = (
    "You are Knuth, a general purpose agent. "
)


async def build_runtime(
    *,
    config_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    db_path: Path | str | None = None,
) -> AgentRuntime:
    config = await load_config(config_path, environ)
    return build_sqlite_runtime(
        inference_client=LiteLLMInferenceClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
        ),
        inference_config=InferenceConfig(timeout_s=config.timeout),
        db_path=db_path,
        section_providers=[
            StaticSectionProvider(SystemSectionSource.BASE, KNUTH_CLI_BASE_IDENTITY),
            StaticSectionProvider(SystemSectionSource.USER, config.system_prompt),
        ],
    )
