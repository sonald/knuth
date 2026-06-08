from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import anyio


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0


async def load_config(
    config_path: Path | str = "knuth.toml",
    environ: Mapping[str, str] | None = None,
) -> Config:
    values = await _read_config_file(Path(config_path))
    source = os.environ if environ is None else environ
    for env_key, config_key in _ENV_TO_CONFIG_KEY.items():
        if env_key in source:
            values[config_key] = source[env_key]

    missing = [
        env_key
        for env_key, config_key in _ENV_TO_CONFIG_KEY.items()
        if config_key != "timeout" and not values.get(config_key)
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Missing required LLM configuration: {joined}")

    timeout = float(values.get("timeout") or 60.0)
    return Config(
        api_key=str(values["api_key"]),
        base_url=str(values["base_url"]),
        model=str(values["model"]),
        timeout=timeout,
    )


_ENV_TO_CONFIG_KEY = {
    "KNUTH_API_KEY": "api_key",
    "KNUTH_BASE_URL": "base_url",
    "KNUTH_MODEL": "model",
    "KNUTH_TIMEOUT": "timeout",
}


async def _read_config_file(config_path: Path) -> dict[str, Any]:
    if not await anyio.Path(config_path).exists():
        return {}

    async with await anyio.open_file(config_path, "rb") as file:
        content = await file.read()
    return dict(tomllib.loads(content.decode("utf-8")))
