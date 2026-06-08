from __future__ import annotations

import argparse
import copy
import json
from typing import NamedTuple, Sequence

from rich.syntax import Syntax
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, ListItem
from textual.widgets import ListView, RichLog, Select, Static
from textual.worker import Worker

from knuth.core.events import InferenceEvent
from knuth_llmd import InferenceConfig, LiteLLMInferenceClient
from llmd_event_probe import DEBUG_TOOL_SCHEMA, _load_config, _messages


class BuiltinPrompt(NamedTuple):
    key: str
    label: str
    prompt: str
    use_debug_tool: bool = False


BUILTIN_PROMPTS: tuple[BuiltinPrompt, ...] = (
    BuiltinPrompt(
        key="short",
        label="Short answer",
        prompt="Say hello in one short sentence.",
    ),
    BuiltinPrompt(
        key="content_stream",
        label="Content stream",
        prompt="Write three short numbered fragments about event streams.",
    ),
    BuiltinPrompt(
        key="reasoning",
        label="Reasoning probe",
        prompt=(
            "Solve 17 * 23. If the model supports a reasoning stream, use it, "
            "then answer with only the final number."
        ),
    ),
    BuiltinPrompt(
        key="tool_call",
        label="Tool call probe",
        prompt='Use the debug_echo tool with text "hello from llmd event tui".',
        use_debug_tool=True,
    ),
)


def prompt_options() -> list[tuple[str, str]]:
    return [(prompt.label, prompt.key) for prompt in BUILTIN_PROMPTS]


def builtin_prompt_by_key(key: object) -> BuiltinPrompt | None:
    for prompt in BUILTIN_PROMPTS:
        if prompt.key == key:
            return prompt
    return None


def default_prompt() -> BuiltinPrompt:
    return BUILTIN_PROMPTS[0]


def event_list_label(event: InferenceEvent, receive_index: int) -> str:
    return f"{receive_index:03d} seq={event.seq:<3} {event.type}"


def event_detail_text(event: InferenceEvent | None) -> str:
    if event is None:
        return "Waiting for llmd events..."
    data = event.model_dump(mode="json", exclude_none=True)
    return json.dumps(data, indent=2, ensure_ascii=False)


class LlmdEventTui(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #controls {
        height: 3;
        layout: horizontal;
        padding: 0 1;
    }

    #prompt-select {
        width: 24;
        margin-right: 1;
    }

    #prompt-input {
        width: 1fr;
        margin-right: 1;
    }

    #debug-tool {
        width: 17;
        margin-right: 1;
    }

    #run-button {
        width: 10;
    }

    #status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #body {
        height: 1fr;
        layout: horizontal;
        padding: 0 1 1 1;
    }

    #event-list {
        width: 44;
        border: solid $accent;
        margin-right: 1;
    }

    #detail {
        width: 1fr;
        border: solid $accent;
    }
    """

    BINDINGS = [
        ("ctrl+r", "run_prompt", "Run"),
        ("escape", "cancel_stream", "Cancel"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.events: list[InferenceEvent] = []
        self.stream_worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        selected = default_prompt()
        initial_prompt = self.args.prompt or selected.prompt
        yield Header(show_clock=True)
        with Vertical():
            with Horizontal(id="controls"):
                yield Select(
                    prompt_options(),
                    value=selected.key,
                    allow_blank=False,
                    id="prompt-select",
                )
                yield Input(
                    value=initial_prompt,
                    placeholder="Prompt",
                    id="prompt-input",
                )
                yield Checkbox(
                    "debug_echo",
                    value=bool(self.args.with_debug_tool),
                    id="debug-tool",
                    compact=True,
                )
                yield Button("Run", variant="primary", id="run-button")
            yield Static("ready", id="status")
            with Horizontal(id="body"):
                yield ListView(id="event-list")
                yield RichLog(id="detail", wrap=True, markup=False, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()
        self.show_event(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-button":
            await self.start_current_prompt()

    async def on_input_submitted(self, _: Input.Submitted) -> None:
        await self.start_current_prompt()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "prompt-select":
            return
        prompt = builtin_prompt_by_key(event.value)
        if prompt is None:
            return
        self.query_one("#prompt-input", Input).value = prompt.prompt
        self.query_one("#debug-tool", Checkbox).value = prompt.use_debug_tool

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        self.show_event(self._index_for_item(event.item))

    async def action_run_prompt(self) -> None:
        await self.start_current_prompt()

    def action_cancel_stream(self) -> None:
        self.cancel_stream()

    async def start_current_prompt(self) -> None:
        prompt = self.query_one("#prompt-input", Input).value.strip()
        if not prompt:
            self.update_status("enter a prompt")
            return
        use_debug_tool = self.query_one("#debug-tool", Checkbox).value
        self.cancel_stream()
        await self.reset_events()
        self.update_status("starting stream")
        self.stream_worker = self.run_worker(
            self.stream_prompt(prompt, use_debug_tool),
            name="llmd-stream",
            exclusive=True,
        )

    async def reset_events(self) -> None:
        self.events.clear()
        await self.query_one("#event-list", ListView).clear()
        self.show_event(None)

    def cancel_stream(self) -> None:
        if self.stream_worker is not None and not self.stream_worker.is_cancelled:
            self.stream_worker.cancel()
            self.update_status("stream cancelled")
        self.stream_worker = None

    async def stream_prompt(self, prompt: str, use_debug_tool: bool) -> None:
        args = copy.copy(self.args)
        args.prompt = prompt
        args.with_debug_tool = use_debug_tool
        try:
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
            tools = [DEBUG_TOOL_SCHEMA] if use_debug_tool else []
            tool_status = " with debug_echo" if use_debug_tool else ""
            self.update_status(f"streaming {config.model}{tool_status}")
            async for event in client.stream(
                messages=_messages(prompt, args.system),
                tools=tools,
                config=inference_config,
            ):
                await self.append_event(event)
            self.update_status(f"complete: {len(self.events)} events")
        except Exception as exc:
            self.update_status(f"error: {exc}")
        finally:
            self.stream_worker = None

    async def append_event(self, event: InferenceEvent) -> None:
        self.events.append(event)
        receive_index = len(self.events)
        item = ListItem(Label(event_list_label(event, receive_index)))
        event_list = self.query_one("#event-list", ListView)
        await event_list.append(item)
        event_list.index = receive_index - 1
        event_list.scroll_end(animate=False, immediate=True)
        self.show_event(receive_index - 1)

    def show_event(self, index: int | None) -> None:
        event = self.events[index] if index is not None and self.events else None
        detail = self.query_one("#detail", RichLog)
        detail.clear()
        text = event_detail_text(event)
        if event is None:
            detail.write(text)
        else:
            detail.write(Syntax(text, "json", word_wrap=True))

    def update_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _index_for_item(self, item: ListItem) -> int | None:
        event_list = self.query_one("#event-list", ListView)
        for index, child in enumerate(event_list.children):
            if child is item:
                return index
        return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    from llmd_event_probe import parse_args as parse_probe_args

    return parse_probe_args(
        argv,
        description=(
            "Open a Textual TUI for llmd InferenceEvent streams with prompt "
            "input, built-in test prompts, an event list, and JSON details."
        ),
    )


async def main_async(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    await LlmdEventTui(args).run_async()


def main() -> None:
    import anyio

    anyio.run(main_async)


if __name__ == "__main__":
    main()
