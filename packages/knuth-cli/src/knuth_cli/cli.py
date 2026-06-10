import argparse
import json
import sys
from collections.abc import Awaitable, Callable

import anyio
from rich.console import Console

from knuth_cli import __version__
from knuth_cli.repl import run_interactive, run_single
from knuth_cli.runtime import build_runtime
from knuth_runtime import AgentRuntime

CommandHandler = Callable[[AgentRuntime, argparse.Namespace], Awaitable[int]]


def main(
    argv: list[str] | None = None,
    runtime_factory: Callable[[], Awaitable[AgentRuntime]] = build_runtime,
) -> int:
    return anyio.run(async_main, argv, runtime_factory)


async def _handle_run(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    console = Console()
    prompt = args.once if args.once is not None else args.prompt
    if prompt is not None:
        return await run_single(runtime, console, prompt)
    return await run_interactive(runtime, console)


async def _handle_events(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    for event in await runtime.events(args.run_id):
        sys.stdout.write(
            json.dumps(event.model_dump(), ensure_ascii=False, default=str) + "\n"
        )
    return 0


async def _handle_status(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    sys.stdout.write(f"{(await runtime.status(args.run_id)).value}\n")
    return 0


async def _handle_tools(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    tools = await runtime.tools()
    for item in tools:
        function = item.get("function", {})
        sys.stdout.write(f"{function.get('name')}\t{function.get('description')}\n")
    return 0


async def _handle_approve(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    approval = await runtime.approve(args.approval_id)
    sys.stdout.write(f"{approval.id}\t{approval.status.value}\n")
    return 0


async def _handle_deny(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    approval = await runtime.deny(args.approval_id)
    sys.stdout.write(f"{approval.id}\t{approval.status.value}\n")
    return 0


async def _handle_resume(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    async with runtime.resume(args.run_id) as session:
        turn = await session.result()
    sys.stdout.write(f"{turn.answer}\n")
    if turn.run_id is not None:
        sys.stdout.write(f"run_id={turn.run_id}\n")
    if turn.status is not None:
        sys.stdout.write(f"status={turn.status.value}\n")
    return 0


_COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "run": _handle_run,
    "events": _handle_events,
    "status": _handle_status,
    "tools": _handle_tools,
    "approve": _handle_approve,
    "deny": _handle_deny,
    "resume": _handle_resume,
}


async def async_main(
    argv: list[str] | None = None,
    runtime_factory: Callable[[], Awaitable[AgentRuntime]] = build_runtime,
) -> int:
    parser = argparse.ArgumentParser(prog="knuth", description="Knuth agent framework")
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run an agent session")
    run_parser.add_argument("prompt", nargs="?", help="Prompt to run once.")
    run_parser.add_argument(
        "--once",
        metavar="PROMPT",
        help="Run one agent turn and exit. Kept for compatibility.",
    )
    subparsers.add_parser("tools", help="Tool commands").add_argument(
        "tool_command", choices=["list"], help="Tool subcommand"
    )
    events_parser = subparsers.add_parser("events", help="Print run events")
    events_parser.add_argument("run_id")
    status_parser = subparsers.add_parser("status", help="Print run status")
    status_parser.add_argument("run_id")
    resume_parser = subparsers.add_parser("resume", help="Resume a paused run")
    resume_parser.add_argument("run_id")
    approve_parser = subparsers.add_parser("approve", help="Approve a pending request")
    approve_parser.add_argument("approval_id")
    deny_parser = subparsers.add_parser("deny", help="Deny a pending request")
    deny_parser.add_argument("approval_id")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    runtime = await runtime_factory()
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 1
    return await handler(runtime, args)


if __name__ == "__main__":
    sys.exit(main())
