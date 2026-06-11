from __future__ import annotations

from pathlib import Path
from typing import Mapping

from knuth_llmd import InferenceConfig, LiteLLMInferenceClient
from knuth_runtime import AgentRuntime, build_sqlite_runtime
from knuth_runtime.debug import DEFAULT_DEBUG_SINK_DIR

from knuth_cli.config import load_config
from knuth_cli.prompts import build_cli_system_sections
from knuth_cli.tools import create_cli_tools


async def build_runtime(
    *,
    config_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    db_path: Path | str | None = None,
    enable_plugins: bool = False,
    debug: bool = False,
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
        section_providers=build_cli_system_sections(config.system_prompt),
        tools=create_cli_tools(Path.cwd()),
        enable_plugins=enable_plugins,
        debug_sink_dir=DEFAULT_DEBUG_SINK_DIR if debug else None,
    )
