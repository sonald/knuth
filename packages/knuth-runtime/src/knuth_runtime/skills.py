from __future__ import annotations

import hashlib

from pydantic import Field

from knuth.core.messages import InferenceMessage, InferenceRole, SystemSection
from knuth.core.messages import SystemSectionSource
from knuth.core.runtime_events import InsertPosition
from knuth.core.types import KnuthModel
from knuth_runtime.context import RunContext, SystemSectionProvider, TapeMessage
from knuth_runtime.middleware import (
    InsertPatch,
    MessageMiddleware,
    MessageMiddlewareCheckpoint,
    MessageMiddlewareContext,
)
from knuth_toold.skills import (
    SkillManager,
    SkillRoot,
    render_skill_system_section_text,
    render_skills_reminder_text,
)


class SkillRuntimeConfig(KnuthModel):
    roots: list[SkillRoot] = Field(default_factory=list)
    hot_reload: bool = True
    hot_reload_debounce_ms: int = 1000


class SkillSystemSectionProvider(SystemSectionProvider):
    async def sections(self, ctx: RunContext) -> list[SystemSection]:
        _ = ctx
        return [
            SystemSection(
                source=SystemSectionSource.SKILL,
                text=render_skill_system_section_text(),
            )
        ]


class SkillReminderMiddleware(MessageMiddleware):
    name = "skill_reminder"
    priority = 5
    checkpoints = {MessageMiddlewareCheckpoint.AFTER_USER_MESSAGE_COMMITTED}

    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    async def process(
        self,
        ctx: MessageMiddlewareContext,
        messages: tuple[TapeMessage, ...],
    ) -> list[InsertPatch]:
        if ctx.turn_start_id is None:
            return []
        snapshot = self._manager.refresh_if_dirty()
        if _has_skill_reminder(messages, snapshot.catalog_digest):
            return []
        content = render_skills_reminder_text(snapshot)
        return [
            InsertPatch(
                position=InsertPosition(
                    kind="before",
                    target_id=ctx.turn_start_id,
                ),
                items=[
                    InferenceMessage(
                        role=InferenceRole.USER,
                        content=content,
                    )
                ],
                metadata={
                    "category": "skill_reminder",
                    "content_hash": _sha256(content),
                    "skill_count": len(snapshot.skills),
                    "catalog_digest": snapshot.catalog_digest,
                    "snapshot_version": snapshot.version,
                    "reason": "catalog_changed",
                },
            )
        ]


def _has_skill_reminder(messages: tuple[TapeMessage, ...], catalog_digest: str) -> bool:
    for item in reversed(messages):
        semantic = item.metadata.get("semantic", {})
        if semantic.get("category") == "skill_reminder":
            return semantic.get("catalog_digest") == catalog_digest
    return False


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "SkillReminderMiddleware",
    "SkillRuntimeConfig",
    "SkillSystemSectionProvider",
]
