from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import anyio
import platformdirs
import yaml


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0


def default_config_path() -> Path:
    return Path(platformdirs.user_data_dir("knuth")) / "llmd" / "knuth.yaml"


async def load_config(
    config_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    path = Path(config_path) if config_path is not None else default_config_path()
    values = await _read_config_file(path)
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
    loaded = yaml.safe_load(content.decode("utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ValueError(
            f"Config file must contain a mapping, got {type(loaded).__name__}"
        )
    return dict(loaded)
