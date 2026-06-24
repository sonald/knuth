from __future__ import annotations

from knuth.core.commands import (
    DEFAULT_BUILTIN_COMMAND_SPECS,
    CommandCatalog,
    build_command_catalog,
)


async def load_command_catalog(
    runtime,
    *,
    best_effort: bool = False,
) -> CommandCatalog:
    skills_fn = getattr(runtime, "skills", None)
    skills = []
    if skills_fn is not None:
        try:
            skills = await skills_fn()
        except Exception:
            if not best_effort:
                raise
    return build_command_catalog(DEFAULT_BUILTIN_COMMAND_SPECS, skills)


def builtin_command_catalog() -> CommandCatalog:
    return build_command_catalog(DEFAULT_BUILTIN_COMMAND_SPECS, [])


__all__ = [
    "builtin_command_catalog",
    "load_command_catalog",
]
