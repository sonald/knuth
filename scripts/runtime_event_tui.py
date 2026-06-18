from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import anyio

from knuth_cli.runtime import build_runtime
from knuth_cli.runtime_event_tui import RuntimeEventTui, RuntimeEventTuiController


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a Textual workbench for Knuth RuntimeEvent debugging."
    )
    parser.add_argument("prompt", nargs="?", default="", help="Initial prompt.")
    parser.add_argument("--run-id", default="", help="Run id to load or reuse.")
    parser.add_argument("--config", type=Path, default=None, help="Agent config path.")
    parser.add_argument("--db-path", type=Path, default=None, help="SQLite db path.")
    parser.add_argument(
        "--enable-plugins",
        action="store_true",
        help="Enable trusted third-party tool plugins.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Also write the runtime debug JSONL sink.",
    )
    return parser.parse_args(argv)


async def main_async(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    runtime = await build_runtime(
        config_path=args.config,
        db_path=args.db_path,
        enable_plugins=args.enable_plugins,
        debug=args.debug,
    )
    controller = RuntimeEventTuiController(runtime)
    await RuntimeEventTui(
        controller,
        initial_prompt=args.prompt,
        initial_run_id=args.run_id,
    ).run_async()


def main() -> None:
    anyio.run(main_async)


if __name__ == "__main__":
    main()
