from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import anyio
import platformdirs
import yaml

from knuth.core.skills import SkillSource
from knuth_toold.skills import SkillRoot


@dataclass(frozen=True)
class AgentConfig:
    api_key: str | None
    base_url: str | None
    model: str
    auth_mode: str = "api_key"
    chatgpt_token_dir: str | None = None
    timeout: float = 60.0
    system_prompt: str | None = None
    skill_roots: list[SkillRoot] | None = None
    skill_hot_reload: bool = True
    skill_hot_reload_debounce_ms: int = 1000


@dataclass(frozen=True)
class AgentSkillConfig:
    roots: list[SkillRoot]
    hot_reload: bool = True
    hot_reload_debounce_ms: int = 1000


def default_config_path() -> Path:
    return Path(platformdirs.user_data_dir("knuth")) / "knuth-cli" / "knuth.yaml"


async def load_config(
    config_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> AgentConfig:
    explicit_config_path = config_path is not None
    path = Path(config_path) if explicit_config_path else default_config_path()
    values = await _read_config_file(path)
    if not explicit_config_path:
        values.update(await _read_dotenv_file(Path.cwd() / ".env"))
    source = os.environ if environ is None else environ
    for env_key, config_key in _ENV_TO_CONFIG_KEY.items():
        if env_key in source:
            values[config_key] = source[env_key]

    return _agent_config_from_values(values)


def load_agent_config_from_env(environ: Mapping[str, str] | None = None) -> AgentConfig:
    source = os.environ if environ is None else environ
    values = {
        config_key: source[env_key]
        for env_key, config_key in _ENV_TO_CONFIG_KEY.items()
        if env_key in source
    }
    return _agent_config_from_values(values)


def _agent_config_from_values(values: Mapping[str, Any]) -> AgentConfig:
    model = str(values.get("model") or "")
    auth_mode = _auth_mode(values.get("auth_mode"), model)
    required_config_keys = {"model"}
    if auth_mode == "api_key":
        required_config_keys.update({"api_key", "base_url"})

    missing = [
        env_key
        for env_key, config_key in _ENV_TO_CONFIG_KEY.items()
        if config_key in required_config_keys and not values.get(config_key)
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Missing required agent configuration: {joined}")

    timeout = float(values.get("timeout") or 60.0)
    system_prompt = values.get("system_prompt")
    skill_config = parse_agent_skill_config(values)
    return AgentConfig(
        api_key=str(values["api_key"]) if values.get("api_key") else None,
        base_url=str(values["base_url"]) if values.get("base_url") else None,
        model=model,
        auth_mode=auth_mode,
        chatgpt_token_dir=(
            str(values["chatgpt_token_dir"])
            if values.get("chatgpt_token_dir")
            else None
        ),
        timeout=timeout,
        system_prompt=str(system_prompt) if system_prompt else None,
        skill_roots=skill_config.roots,
        skill_hot_reload=skill_config.hot_reload,
        skill_hot_reload_debounce_ms=skill_config.hot_reload_debounce_ms,
    )


_SKILL_ENV_TO_CONFIG_KEY = {
    "KNUTH_SKILL_ROOTS": "skill_roots",
    "KNUTH_SKILL_HOT_RELOAD": "skill_hot_reload",
    "KNUTH_SKILL_HOT_RELOAD_DEBOUNCE_MS": "skill_hot_reload_debounce_ms",
}

_ENV_TO_CONFIG_KEY = {
    "KNUTH_API_KEY": "api_key",
    "KNUTH_BASE_URL": "base_url",
    "KNUTH_MODEL": "model",
    "KNUTH_AUTH_MODE": "auth_mode",
    "KNUTH_CHATGPT_TOKEN_DIR": "chatgpt_token_dir",
    "KNUTH_TIMEOUT": "timeout",
    "KNUTH_SYSTEM_PROMPT": "system_prompt",
    **_SKILL_ENV_TO_CONFIG_KEY,
}

_OPTIONAL_CONFIG_KEYS = {
    "timeout",
    "system_prompt",
    "auth_mode",
    "chatgpt_token_dir",
    "skill_roots",
    "skill_hot_reload",
    "skill_hot_reload_debounce_ms",
}


def _auth_mode(value: object, model: str) -> str:
    mode = str(value or "").strip()
    if mode:
        if mode not in {"api_key", "chatgpt"}:
            raise ValueError("KNUTH_AUTH_MODE must be 'api_key' or 'chatgpt'")
        return mode
    if model.startswith("chatgpt/"):
        return "chatgpt"
    return "api_key"


def _default_skill_roots() -> list[SkillRoot]:
    return [
        SkillRoot(
            source=SkillSource.PROJECT,
            path=str(Path.cwd() / ".knuth" / "skills"),
        ),
        SkillRoot(
            source=SkillSource.USER,
            path=str(Path.home() / ".agents" / "skills"),
        ),
    ]


def parse_agent_skill_config(
    values: Mapping[str, Any] | None = None,
) -> AgentSkillConfig:
    raw_values = dict(values or {})
    debounce_ms = _parse_non_negative_int(
        raw_values.get("skill_hot_reload_debounce_ms"),
        default=1000,
        field_name="skill_hot_reload_debounce_ms",
    )
    return AgentSkillConfig(
        roots=_parse_skill_roots(raw_values),
        hot_reload=_parse_bool(raw_values.get("skill_hot_reload", True)),
        hot_reload_debounce_ms=debounce_ms,
    )


def load_agent_skill_config_from_env(
    environ: Mapping[str, str] | None = None,
) -> AgentSkillConfig:
    source = os.environ if environ is None else environ
    values = {
        config_key: source[env_key]
        for env_key, config_key in _SKILL_ENV_TO_CONFIG_KEY.items()
        if env_key in source
    }
    return parse_agent_skill_config(values)


def _parse_skill_roots(values: dict[str, Any]) -> list[SkillRoot]:
    raw = values.get("skill_roots")
    if raw is None:
        return _default_skill_roots()
    if isinstance(raw, str):
        if not raw:
            return []
        return [
            SkillRoot(source=SkillSource.HOST, path=path)
            for path in raw.split(os.pathsep)
            if path
        ]
    if not isinstance(raw, list):
        raise ValueError("skill_roots must be a list or path-separated string")
    roots: list[SkillRoot] = []
    for item in raw:
        if isinstance(item, str):
            roots.append(SkillRoot(source=SkillSource.HOST, path=item))
            continue
        if isinstance(item, Mapping):
            source = item.get("source", "host")
            path = item.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("skill_roots entries must include a non-empty path")
            roots.append(SkillRoot(source=SkillSource(source), path=path))
            continue
        raise ValueError("skill_roots entries must be strings or mappings")
    return roots


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _parse_non_negative_int(
    value: Any,
    *,
    default: int,
    field_name: str,
) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return parsed


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


async def _read_dotenv_file(path: Path) -> dict[str, str]:
    if not await anyio.Path(path).exists():
        return {}

    values: dict[str, str] = {}
    async with await anyio.open_file(path, encoding="utf-8") as file:
        async for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config_key = _ENV_TO_CONFIG_KEY.get(key.strip())
            if config_key is None:
                continue
            values[config_key] = _strip_dotenv_quotes(value.strip())
    return values


def _strip_dotenv_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
