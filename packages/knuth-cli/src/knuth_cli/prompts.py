from __future__ import annotations

from knuth.core.messages import SystemSectionSource
from knuth_runtime import StaticSectionProvider


KNUTH_CLI_ROLE_PROMPT = """# ROLE
You are **Knuth Shell**, a local AI shell agent running with the user's current directory as your working directory.

You can use shell-style tools to inspect files, edit files, run commands, and help the user complete local development tasks. Prefer using tools when they can ground the answer in current local state.
"""


def build_cli_system_sections(user_prompt: str | None = None) -> list[StaticSectionProvider]:
    sections = [
        StaticSectionProvider(SystemSectionSource.BASE, KNUTH_CLI_ROLE_PROMPT),
    ]
    if user_prompt:
        sections.append(StaticSectionProvider(SystemSectionSource.USER, user_prompt))
    return sections
