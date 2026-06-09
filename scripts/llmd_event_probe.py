from __future__ import annotations

import argparse
import os
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import anyio
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from knuth.core.events import InferenceEvent
from knuth.core.messages import InferenceMessage, InferenceRole
from knuth_cli.config import AgentConfig, load_config
from knuth_llmd import InferenceConfig, LiteLLMInferenceClient


DEBUG_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "debug_echo",
        "description": "Echo text back for llmd event probing.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to echo.",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}


def parse_args(
    argv: Sequence[str] | None = None,
    *,
    description: str | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=description
        or (
            "Stream one llmd request and print every InferenceEvent in receive order "
            "with Rich."
        )
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello in one short sentence.",
        help="User prompt sent to llmd.",
    )
    parser.add_argument(
        "--system",
        default=None,
        help="Optional system message.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to agent YAML config. Defaults to the knuth-cli user config.",
    )
    parser.add_argument("--api-key", default=None, help="Override KNUTH_API_KEY.")
    parser.add_argument("--base-url", default=None, help="Override KNUTH_BASE_URL.")
    parser.add_argument("--model", default=None, help="Override KNUTH_MODEL.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Override KNUTH_TIMEOUT / client timeout in seconds.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Optional model temperature.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Optional output token limit.",
    )
    parser.add_argument(
        "--run-id",
        default="llmd-event-probe",
        help="Run id attached to emitted inference events.",
    )
    parser.add_argument(
        "--with-debug-tool",
        action="store_true",
        help="Expose a debug_echo tool schema so tool-call events can be observed.",
    )
    return parser.parse_args(argv)


def event_json_data(event: InferenceEvent) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude_none=True)


def render_event(console: Console, event: InferenceEvent, receive_index: int) -> None:
    data = event_json_data(event)
    title = f"#{receive_index} {event.type}"
    subtitle = f"seq={event.seq} generation_id={event.generation_id}"
    console.print(
        Panel(
            JSON.from_data(data, indent=2),
            title=title,
            subtitle=subtitle,
            expand=False,
        )
    )


def render_summary(console: Console, events: Sequence[InferenceEvent]) -> None:
    counts = Counter(event.type for event in events)
    table = Table(title="llmd event summary")
    table.add_column("event type", style="cyan")
    table.add_column("count", justify="right", style="magenta")
    for event_type, count in counts.items():
        table.add_row(event_type, str(count))
    table.caption = f"total events: {len(events)}"
    console.print(table)


def _messages(prompt: str, system: str | None) -> list[InferenceMessage]:
    messages: list[InferenceMessage] = []
    if system:
        messages.append(InferenceMessage(role=InferenceRole.SYSTEM, content=system))
    messages.append(InferenceMessage(role=InferenceRole.USER, content=prompt))
    return messages


async def _load_config(args: argparse.Namespace) -> AgentConfig:
    environ = dict(os.environ)
    if args.api_key is not None:
        environ["KNUTH_API_KEY"] = args.api_key
    if args.base_url is not None:
        environ["KNUTH_BASE_URL"] = args.base_url
    if args.model is not None:
        environ["KNUTH_MODEL"] = args.model
    if args.timeout is not None:
        environ["KNUTH_TIMEOUT"] = str(args.timeout)
    return await load_config(args.config, environ)


async def run_probe(args: argparse.Namespace, console: Console) -> list[InferenceEvent]:
    config = await _load_config(args)
    client = LiteLLMInferenceClient(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
    )
    inference_config = InferenceConfig(
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
        timeout_s=args.timeout,
        run_id=args.run_id,
    )
    tools = [DEBUG_TOOL_SCHEMA] if args.with_debug_tool else []
    events: list[InferenceEvent] = []

    console.print(f"[bold]model[/bold]: {config.model}")
    console.print(f"[bold]base_url[/bold]: {config.base_url}")
    console.print(f"[bold]run_id[/bold]: {args.run_id}")
    if tools:
        console.print("[bold]tools[/bold]: debug_echo")
    console.rule("[bold]stream[/bold]")

    async for event in client.stream(
        messages=_messages(args.prompt, args.system),
        tools=tools,
        config=inference_config,
    ):
        events.append(event)
        render_event(console, event, len(events))

    console.rule("[bold]summary[/bold]")
    render_summary(console, events)
    return events


async def main_async(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console()
    try:
        await run_probe(args, console)
    except Exception as exc:
        console.print(f"[bold red]llmd event probe failed:[/bold red] {exc}")
        return 2
    return 0


def main() -> None:
    raise SystemExit(anyio.run(main_async))


if __name__ == "__main__":
    main()
