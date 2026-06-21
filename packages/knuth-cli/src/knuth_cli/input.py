"""CLI-owned input adapters for the interactive REPL."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal, Protocol, TextIO

import anyio
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.keys import Keys
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import KeyBindingsBase
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.text import Text

from knuth_cli.input_history import PromptHistory


@dataclass(frozen=True)
class InputResult:
    kind: Literal["text", "cancelled", "eof"]
    text: str = ""

    @classmethod
    def text_input(cls, text: str) -> "InputResult":
        return cls("text", text)

    @classmethod
    def cancelled(cls) -> "InputResult":
        return cls("cancelled")

    @classmethod
    def eof(cls) -> "InputResult":
        return cls("eof")


class PromptInput(Protocol):
    records_history: bool

    async def read_prompt(self, prompt: str) -> InputResult: ...

    async def read_approval(self, prompt: str) -> InputResult: ...


class PromptToolkitInput:
    """TTY input backed by prompt-toolkit."""

    records_history = True

    def __init__(
        self,
        *,
        history: PromptHistory,
        completer=None,
        prompt_session: PromptSession | None = None,
        approval_session: PromptSession | None = None,
    ) -> None:
        self._prompt_session = prompt_session or PromptSession(
            editing_mode=EditingMode.EMACS,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_in_thread=True,
            complete_while_typing=False,
            enable_history_search=False,
            multiline=True,
            prompt_continuation="      ... ",
            wrap_lines=True,
            mouse_support=False,
            key_bindings=_build_prompt_key_bindings(history),
            style=_STYLE,
        )
        self._approval_session = approval_session or PromptSession(
            editing_mode=EditingMode.EMACS,
            multiline=False,
            enable_history_search=False,
            complete_while_typing=False,
            mouse_support=False,
            style=_STYLE,
        )

    async def read_prompt(self, prompt: str) -> InputResult:
        return await self._read_prompt_toolkit(self._prompt_session, prompt)

    async def read_approval(self, prompt: str) -> InputResult:
        return await self._read_prompt_toolkit(self._approval_session, prompt)

    async def _read_prompt_toolkit(
        self, session: PromptSession, prompt: str
    ) -> InputResult:
        try:
            text = await session.prompt_async(prompt)
        except KeyboardInterrupt:
            return InputResult.cancelled()
        except EOFError:
            return InputResult.eof()
        return InputResult.text_input(text.replace("\t", "    "))


class StreamInput:
    """Line-oriented input for non-TTY and the first replacement slice."""

    records_history = False

    def __init__(
        self,
        console: Console,
        *,
        input_stream: TextIO | None = None,
    ) -> None:
        self._console = console
        self._input_stream = input_stream

    async def read_prompt(self, prompt: str) -> InputResult:
        return await self._read_stream_line(prompt)

    async def read_approval(self, prompt: str) -> InputResult:
        return await self._read_stream_line(prompt)

    async def _read_stream_line(self, prompt: str) -> InputResult:
        while True:
            self._console.print(Text(prompt), end="")
            try:
                line = await anyio.to_thread.run_sync(self._readline)
            except (KeyboardInterrupt, anyio.get_cancelled_exc_class()):
                return InputResult.cancelled()
            except UnicodeDecodeError:
                self._console.print(
                    Text(
                        "Could not decode input as UTF-8; please try again.",
                        style="yellow",
                    )
                )
                continue
            except Exception:
                return InputResult.eof()

            if line == "":
                return InputResult.eof()
            return InputResult.text_input(line.rstrip("\n"))

    def _readline(self) -> str:
        stream = self._input_stream if self._input_stream is not None else sys.stdin
        return stream.readline()


_STYLE = Style.from_dict(
    {
        "completion-menu.completion": "bg:#303030 #ffffff",
        "completion-menu.completion.current": "bg:#5f5f87 #ffffff",
        "auto-suggestion": "#666666",
    }
)


class _HistorySearchState:
    def __init__(self, history: PromptHistory) -> None:
        self.history = history
        self.active = False
        self.draft = ""
        self.query = ""
        self.matches: list[str] = []
        self.index = 0

    def start_or_next(self, buffer) -> None:
        if not self.active:
            self.active = True
            self.draft = buffer.text
            self.query = ""
            self.matches = list(self.history.load_history_strings())
            self.index = 0
            return
        if self.matches:
            self.index = (self.index + 1) % len(self.matches)
            self._apply(buffer)

    def append(self, buffer, text: str) -> None:
        self.query += text
        self.index = 0
        self._refresh(buffer)

    def backspace(self, buffer) -> None:
        if not self.query:
            return
        self.query = self.query[:-1]
        self.index = 0
        self._refresh(buffer)

    def accept(self) -> None:
        self.active = False

    def cancel(self, buffer) -> None:
        self.active = False
        buffer.set_document(
            Document(self.draft, cursor_position=len(self.draft)),
            bypass_readonly=True,
        )

    def _refresh(self, buffer) -> None:
        query = self.query.casefold()
        self.matches = [
            item
            for item in self.history.load_history_strings()
            if query in item.casefold()
        ]
        if self.matches:
            self._apply(buffer)
            return
        buffer.set_document(
            Document(self.draft, cursor_position=len(self.draft)),
            bypass_readonly=True,
        )

    def _apply(self, buffer) -> None:
        text = self.matches[self.index]
        buffer.set_document(
            Document(text, cursor_position=len(text)),
            bypass_readonly=True,
        )


def _build_prompt_key_bindings(history: PromptHistory) -> KeyBindingsBase:
    bindings = KeyBindings()
    history_search = _HistorySearchState(history)

    @Condition
    def search_is_active() -> bool:
        return history_search.active

    @bindings.add("enter")
    def _(event) -> None:
        if history_search.active:
            history_search.accept()
        event.app.exit(result=event.app.current_buffer.text)

    @bindings.add("escape", "enter")
    def _(event) -> None:
        event.app.current_buffer.insert_text("\n")

    @bindings.add("tab")
    def _(event) -> None:
        if history_search.active:
            history_search.accept()
            return
        buffer = event.app.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            buffer.apply_completion(buffer.complete_state.current_completion)
            return
        buffer.start_completion(select_first=True)

    @bindings.add("escape", filter=search_is_active)
    def _(event) -> None:
        history_search.accept()

    @bindings.add("c-r")
    def _(event) -> None:
        history_search.start_or_next(event.app.current_buffer)

    @bindings.add("c-c")
    def _(event) -> None:
        if history_search.active:
            history_search.cancel(event.app.current_buffer)
            return
        event.app.exit(exception=KeyboardInterrupt)

    @bindings.add("backspace", filter=search_is_active)
    @bindings.add("c-h", filter=search_is_active)
    def _(event) -> None:
        history_search.backspace(event.app.current_buffer)

    @bindings.add(Keys.Any, filter=search_is_active)
    def _(event) -> None:
        data = event.data
        if data:
            history_search.append(event.app.current_buffer, data)

    return bindings
