from __future__ import annotations

from pathlib import Path

from knuth.core.messages import SystemSectionSource
from knuth_runtime import (
    ContextCompactionMiddleware,
    MessageMiddleware,
    ObservationCondensationMiddleware,
    StaticSectionProvider,
)


KNUTH_CLI_ROLE_PROMPT = """# ROLE
You are **Knuth Shell**, a local AI shell agent running with the user's current directory as your working directory.

You can use shell-style tools to inspect files, edit files, run commands, and help the user complete local development tasks. Prefer using tools when they can ground the answer in current local state.
Use `glob` to find files by name and `grep` to search file contents before using `shell` for read-only search tasks.
"""


def build_cli_system_sections(
    user_prompt: str | None = None,
    workspace: Path | str | None = None,
) -> list[StaticSectionProvider]:
    sections = [
        StaticSectionProvider(SystemSectionSource.BASE, KNUTH_CLI_ROLE_PROMPT),
    ]
    if user_prompt:
        sections.append(StaticSectionProvider(SystemSectionSource.USER, user_prompt))
    root = Path(workspace) if workspace is not None else Path.cwd()
    agents_path = root / "AGENTS.md"
    if agents_path.is_file():
        text = agents_path.read_text(encoding="utf-8")
        if text.strip():
            sections.append(
                StaticSectionProvider(
                    SystemSectionSource.USER,
                    f"# {agents_path.name}\n{text}",
                )
            )
    return sections


def build_cli_message_middlewares() -> list[MessageMiddleware]:
    return [
        ObservationCondensationMiddleware(),
        ContextCompactionMiddleware(),
    ]
