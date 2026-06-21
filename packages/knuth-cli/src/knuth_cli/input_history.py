"""Persistent prompt history for the CLI REPL."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import platformdirs
from prompt_toolkit.history import History


@dataclass(frozen=True)
class PromptHistoryRecord:
    text: str
    project_key: str
    cwd: str
    session_id: str
    timestamp: str
    kind: str = "prompt"

    def to_json(self) -> str:
        return json.dumps(
            {
                "text": self.text,
                "project_key": self.project_key,
                "cwd": self.cwd,
                "session_id": self.session_id,
                "timestamp": self.timestamp,
                "kind": self.kind,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def default_history_path() -> Path:
    return Path(platformdirs.user_data_dir("knuth")) / "knuth-cli" / "history.jsonl"


def resolve_project_key(cwd: Path | None = None) -> str:
    current = (cwd or Path.cwd()).resolve(strict=False)
    for candidate in (current, *current.parents):
        if candidate.joinpath(".git").exists():
            return str(candidate.resolve(strict=False))
    return str(current)


class PromptHistory(History):
    """Append-only JSONL history with project-scoped navigation."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        cwd: Path | None = None,
        session_id: str | None = None,
        read_limit: int = 1000,
    ) -> None:
        super().__init__()
        self.path = path or default_history_path()
        self.cwd = (cwd or Path.cwd()).resolve(strict=False)
        self.project_key = resolve_project_key(self.cwd)
        self.session_id = session_id or uuid.uuid4().hex
        self.read_limit = read_limit
        self._navigation_strings: list[str] = []
        self._latest_event_text: str | None = None
        self._persistent_available = True
        self._reload_from_disk()

    def start_new_session(self) -> None:
        self.session_id = uuid.uuid4().hex

    def append_prompt(self, text: str) -> None:
        if not text:
            return
        if self._latest_event_text == text:
            return

        self._latest_event_text = text
        self._remember_for_navigation(text)
        record = PromptHistoryRecord(
            text=text,
            project_key=self.project_key,
            cwd=str(self.cwd),
            session_id=self.session_id,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        if not self._persistent_available:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(record.to_json())
                file.write("\n")
        except OSError:
            self._persistent_available = False

    def load_history_strings(self) -> Iterable[str]:
        yield from self._navigation_strings

    def store_string(self, string: str) -> None:
        # REPL semantic classification owns history writes. Prompt-toolkit
        # automatic writes are disabled, and this method intentionally does
        # nothing if called accidentally.
        return None

    def _reload_from_disk(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError:
            self._persistent_available = False
            return

        recent_lines = lines[-self.read_limit :]
        latest_event_text: str | None = None
        navigation: list[str] = []
        seen: set[str] = set()

        for line in reversed(recent_lines):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw.get("kind") != "prompt":
                continue
            if raw.get("project_key") != self.project_key:
                continue
            text = raw.get("text")
            if not isinstance(text, str) or not text:
                continue
            if latest_event_text is None:
                latest_event_text = text
            if text in seen:
                continue
            seen.add(text)
            navigation.append(text)

        self._latest_event_text = latest_event_text
        self._navigation_strings = navigation
        self._loaded_strings = list(navigation)
        self._loaded = True

    def _remember_for_navigation(self, text: str) -> None:
        self._navigation_strings = [
            item for item in self._navigation_strings if item != text
        ]
        self._navigation_strings.insert(0, text)
        if len(self._navigation_strings) > self.read_limit:
            self._navigation_strings = self._navigation_strings[: self.read_limit]
        self._loaded_strings = list(self._navigation_strings)
        self._loaded = True
