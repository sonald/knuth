"""Serve the Knuth IM AG-UI backend."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

import uvicorn
from knuth_agui import create_agui_client_tool_provider, create_app

from knuth_im.runtime_factory import build_runtime, load_dotenv


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    db_path: Path | None
    workspace: Path | None
    auth_token: str | None
    env_file: Path | None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return int(raw)


def _absolute_path(raw: str | None) -> Path | None:
    if raw in (None, ""):
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def parse_server_config(argv: Sequence[str] | None = None) -> ServerConfig:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument(
        "--env-file",
        default=os.environ.get("KNUTH_IM_ENV_FILE", ".env"),
        help="Path to a dotenv-style config file. Defaults to .env.",
    )
    env_args, _ = env_parser.parse_known_args(argv)
    env_file = _absolute_path(env_args.env_file)
    if env_file is not None:
        load_dotenv(env_file)

    parser = argparse.ArgumentParser(description="Serve the Knuth IM AG-UI backend.")
    parser.add_argument(
        "--host",
        default=os.environ.get("KNUTH_IM_HOST", "127.0.0.1"),
        help="Host interface for uvicorn. Defaults to KNUTH_IM_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("KNUTH_IM_PORT", 8000),
        help="Port for uvicorn. Use 0 only when the caller can discover it.",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("KNUTH_IM_DB_PATH"),
        help="SQLite ledger path. Defaults to ~/.knuth/knuth-im.db.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("KNUTH_IM_WORKSPACE"),
        help="Workspace directory used as the backend process working directory.",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("KNUTH_IM_AUTH_TOKEN"),
        help="Optional local API bearer token for sidecar-managed backends.",
    )
    parser.add_argument(
        "--env-file",
        default=env_args.env_file,
        help="Path to a dotenv-style config file. Defaults to .env.",
    )
    args = parser.parse_args(argv)

    workspace = _absolute_path(args.workspace)
    if workspace is not None and not workspace.is_dir():
        parser.error(f"--workspace must be an existing directory: {workspace}")

    return ServerConfig(
        host=args.host,
        port=args.port,
        db_path=_absolute_path(args.db_path),
        workspace=workspace,
        auth_token=args.auth_token or None,
        env_file=_absolute_path(args.env_file),
    )


def main(argv: Sequence[str] | None = None) -> None:
    config = parse_server_config(argv)
    if config.workspace is not None:
        os.chdir(config.workspace)
    client_tool_provider = create_agui_client_tool_provider()
    app = create_app(
        build_runtime(
            db_path=config.db_path,
            tool_providers=[client_tool_provider],
        ),
        auth_token=config.auth_token,
        client_tool_provider=client_tool_provider,
    )
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
