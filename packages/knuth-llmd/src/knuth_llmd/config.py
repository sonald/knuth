from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import anyio


@dataclass(frozen=True)
class LlmConfig:
    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0


async def load_llm_config(
    env_path: Path | str = ".env",
    environ: Mapping[str, str] | None = None,
) -> LlmConfig:
    values = await _read_env_file(Path(env_path))
    source = os.environ if environ is None else environ
    for key in ("KNUTH_API_KEY", "KNUTH_BASE_URL", "KNUTH_MODEL", "KNUTH_TIMEOUT"):
        if key in source:
            values[key] = source[key]

    missing = [
        key
        for key in ("KNUTH_API_KEY", "KNUTH_BASE_URL", "KNUTH_MODEL")
        if not values.get(key)
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Missing required LLM configuration: {joined}")

    timeout = float(values.get("KNUTH_TIMEOUT") or 60.0)
    return LlmConfig(
        api_key=values["KNUTH_API_KEY"],
        base_url=values["KNUTH_BASE_URL"],
        model=values["KNUTH_MODEL"],
        timeout=timeout,
    )


async def _read_env_file(env_path: Path) -> dict[str, str]:
    if not await anyio.Path(env_path).exists():
        return {}

    values: dict[str, str] = {}
    async with await anyio.open_file(env_path, encoding="utf-8") as file:
        content = await file.read()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_quotes(value.strip())
        if key:
            values[key] = value
    return values


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
