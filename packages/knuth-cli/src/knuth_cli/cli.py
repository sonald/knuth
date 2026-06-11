import argparse
import inspect
import json
import sys
from collections.abc import Awaitable, Callable

import anyio
from rich.console import Console
from rich.text import Text

from knuth_cli import __version__
from knuth_cli.repl import run_interactive, run_resume, run_single
from knuth_cli.runtime import build_runtime
from knuth_runtime import AgentRuntime, LedgerError

CommandHandler = Callable[[AgentRuntime, argparse.Namespace], Awaitable[int]]

_EXIT_INTERRUPTED = 130


def _factory_kwargs(runtime_factory, args: argparse.Namespace) -> dict:
    """Pass global flags through to factories that accept them; test fakes
    with narrower signatures keep working unchanged."""
    params = inspect.signature(runtime_factory).parameters
    return {
        name: getattr(args, name)
        for name in ("enable_plugins", "debug")
        if name in params and hasattr(args, name)
    }


def main(
    argv: list[str] | None = None,
    runtime_factory: Callable[[], Awaitable[AgentRuntime]] = build_runtime,
) -> int:
    try:
        return anyio.run(async_main, argv, runtime_factory)
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return _EXIT_INTERRUPTED


async def _handle_run(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    console = Console()
    prompt = args.once if args.once is not None else args.prompt
    if prompt is not None:
        if not prompt.strip():
            sys.stderr.write("error: prompt is empty\n")
            return 2
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


async def _handle_runs(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    console = Console()
    runs = await runtime.runs(args.limit)
    if not runs:
        console.print(Text("No runs.", style="dim"))
        return 0
    for run in runs:
        query = run.query.replace("\n", " ")
        if len(query) > 60:
            query = query[:59] + "…"
        console.print(
            Text(run.id, style="bold")
            + Text(f"  {run.status.value:<17}", style="cyan")
            + Text(f"{run.updated_at}  ", style="dim")
            + Text(query)
        )
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
    return await run_resume(runtime, Console(), args.run_id)


async def _handle_resolve(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    invocation = await runtime.resolve_unknown(
        args.tool_call_id, args.outcome, args.note
    )
    sys.stdout.write(f"{invocation.tool_call_id}\t{invocation.status.value}\n")
    return 0


async def _handle_approvals(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    for approval in await runtime.pending_approvals(args.run_id):
        sys.stdout.write(
            f"{approval.id}\t{approval.run_id}\t{approval.title}\n"
        )
    return 0


async def _handle_admin(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    if args.admin_command == "refold":
        stats = await runtime.refold()
        sys.stdout.write(
            f"refolded {stats.runs} runs from {stats.events} events\n"
        )
    return 0


async def _handle_recover(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    reports = await runtime.recover_crashed_runs(args.run_id)
    if not reports:
        sys.stdout.write("no crashed runs\n")
        return 0
    for report in reports:
        sys.stdout.write(
            f"{report.run_id}\tpaused\tfailed={report.failed}"
            f"\tunknown={report.unknown}\n"
        )
        if report.unknown:
            sys.stdout.write(
                "  resolve unknown outcomes with"
                " `knuth resolve <tool_call_id> --outcome ...`\n"
            )
    return 0


_COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "run": _handle_run,
    "runs": _handle_runs,
    "events": _handle_events,
    "status": _handle_status,
    "tools": _handle_tools,
    "approve": _handle_approve,
    "deny": _handle_deny,
    "resume": _handle_resume,
    "resolve": _handle_resolve,
    "approvals": _handle_approvals,
    "admin": _handle_admin,
    "recover": _handle_recover,
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Write the full event stream (including transient reasoning and"
        " raw deltas) to ~/.knuth/debug/<run_id>.jsonl",
    )
    parser.add_argument(
        "--enable-plugins",
        action="store_true",
        dest="enable_plugins",
        help="Discover third-party tools via entry points. Runs plugin code"
        " in-process; enable only for plugins you trust.",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run an agent session")
    run_parser.add_argument("prompt", nargs="?", help="Prompt to run once.")
    run_parser.add_argument(
        "--once",
        metavar="PROMPT",
        help="Run one agent turn and exit. Kept for compatibility.",
    )
    subparsers.add_parser("runs", help="List recent runs").add_argument(
        "--limit", type=int, default=20, help="Max runs to list"
    )
    subparsers.add_parser("tools", help="Tool commands").add_argument(
        "tool_command",
        nargs="?",
        choices=["list"],
        default="list",
        help="Tool subcommand",
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
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve an UNKNOWN external-write tool outcome after a crash",
    )
    resolve_parser.add_argument("tool_call_id")
    resolve_parser.add_argument(
        "--outcome", choices=["succeeded", "failed"], required=True
    )
    resolve_parser.add_argument("--note", default=None)
    approvals_parser = subparsers.add_parser(
        "approvals", help="List pending approvals"
    )
    approvals_parser.add_argument("--run-id", dest="run_id", default=None)
    admin_parser = subparsers.add_parser("admin", help="Maintenance commands")
    admin_subparsers = admin_parser.add_subparsers(
        dest="admin_command", required=True
    )
    admin_subparsers.add_parser(
        "refold", help="Rebuild derived projections from the event log"
    )
    recover_parser = subparsers.add_parser(
        "recover",
        help="Settle in-flight work left by a crashed process and pause"
        " the affected runs",
    )
    recover_parser.add_argument("run_id", nargs="?", default=None)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        runtime = await runtime_factory(**_factory_kwargs(runtime_factory, args))
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.stderr.write(
            "Set KNUTH_API_KEY / KNUTH_BASE_URL / KNUTH_MODEL or create the config file.\n"
        )
        return 1
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 1
    try:
        return await handler(runtime, args)
    except KeyError as exc:
        sys.stderr.write(f"error: unknown run or approval id: {exc.args[0]}\n")
        return 1
    except LedgerError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
