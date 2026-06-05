import argparse
import json
import sys
from collections.abc import Awaitable, Callable
from typing import TextIO

import anyio

from knuth.core.types import RunStatus
from knuth_cli import __version__
from knuth_runtime import AgentRuntime, AgentTurn, build_default_runtime

CommandHandler = Callable[[AgentRuntime, argparse.Namespace], Awaitable[int]]


def main(
    argv: list[str] | None = None,
    runtime_factory: Callable[[], Awaitable[AgentRuntime]] = build_default_runtime,
) -> int:
    return anyio.run(async_main, argv, runtime_factory)


async def _handle_run(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    prompt = args.once if args.once is not None else args.prompt
    if prompt is not None:
        await _print_turn(await runtime.run_once(prompt), sys.stdout)
        return 0
    return await _run_repl(runtime, sys.stdin, sys.stdout)


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
    await _print_turn(await runtime.resume(args.run_id), sys.stdout)
    return 0


async def _print_turn(turn: AgentTurn, output_stream: TextIO) -> None:
    await _write(output_stream, f"{turn.answer}\n")
    if turn.run_id is not None:
        await _write(output_stream, f"run_id={turn.run_id}\n")
    if turn.status is not None:
        await _write(output_stream, f"status={turn.status.value}\n")
    await _flush(output_stream)


async def _run_repl(
    runtime: AgentRuntime, input_stream: TextIO, output_stream: TextIO
) -> int:
    await _write(output_stream, "Knuth agent ready. Type /exit to quit.\n")
    while True:
        await _write(output_stream, "knuth> ")
        await _flush(output_stream)
        line = await anyio.to_thread.run_sync(input_stream.readline)
        if line == "":
            await _write(output_stream, "\n")
            return 0
        prompt = line.strip()
        if prompt in {"/exit", "/quit"}:
            return 0
        if not prompt:
            continue
        turn = await runtime.run_once(prompt)
        await _write(output_stream, f"{turn.answer}\n")
        if turn.status in {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_USER}:
            await _write(output_stream, f"[{turn.status.value}] run_id={turn.run_id}\n")
        await _flush(output_stream)


async def _write(stream: TextIO, value: str) -> None:
    await anyio.to_thread.run_sync(stream.write, value)


async def _flush(stream: TextIO) -> None:
    await anyio.to_thread.run_sync(stream.flush)


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
    runtime_factory: Callable[[], Awaitable[AgentRuntime]] = build_default_runtime,
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
        "tool_command", choices=["list", "refresh"], help="Tool subcommand"
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
