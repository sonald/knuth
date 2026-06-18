from __future__ import annotations

import hashlib
import re

from pydantic import Field

from knuth.core.messages import InferenceMessage, InferenceRole, SystemSection
from knuth.core.messages import SystemSectionSource
from knuth.core.runtime_events import TapePosition
from knuth.core.types import KnuthModel
from knuth_runtime.context import RunContext, SystemSectionProvider
from knuth_runtime.middleware import (
    InsertPatch,
    MessageMiddleware,
    MessageMiddlewareCheckpoint,
    MessageMiddlewareContext,
)
from knuth_toold.skills import (
    SkillManager,
    SkillRoot,
    render_skill_change_notice_text,
    render_skill_system_section_text,
    render_skills_reminder_text,
)

_NOTICE_DIGEST_RE = re.compile(
    r'<knuth-skill-notice\s+catalog-digest="([^"]+)">'
)


class SkillRuntimeConfig(KnuthModel):
    roots: list[SkillRoot] = Field(default_factory=list)
    hot_reload: bool = True
    hot_reload_debounce_ms: int = 1000


class SkillNoticeState:
    """Process-local per-run baseline for detecting catalog changes after a request."""

    def __init__(self) -> None:
        self._last_request_catalog_digest: dict[str, str] = {}

    def remember_model_request(self, run_id: str, catalog_digest: str) -> None:
        self._last_request_catalog_digest[run_id] = catalog_digest

    def last_model_request_digest(self, run_id: str) -> str | None:
        return self._last_request_catalog_digest.get(run_id)


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
    checkpoints = {MessageMiddlewareCheckpoint.BEFORE_MODEL_REQUEST}

    def __init__(
        self,
        manager: SkillManager,
        notice_state: SkillNoticeState | None = None,
    ) -> None:
        self._manager = manager
        self._notice_state = notice_state or SkillNoticeState()

    async def process(
        self,
        ctx: MessageMiddlewareContext,
        tape,
    ):
        _ = ctx, tape
        snapshot = self._manager.refresh_if_dirty()
        self._notice_state.remember_model_request(ctx.run_id, snapshot.catalog_digest)
        content = render_skills_reminder_text(snapshot)
        return [
            InsertPatch(
                position=TapePosition(
                    kind="boundary",
                    boundary="conversation_start",
                ),
                items=[
                    InferenceMessage(
                        role=InferenceRole.USER,
                        content=content,
                    )
                ],
                durable=False,
                metadata={
                    "content_hash": _sha256(content),
                    "skill_count": len(snapshot.skills),
                    "catalog_digest": snapshot.catalog_digest,
                },
            )
        ]


class SkillChangeNoticeMiddleware(MessageMiddleware):
    name = "skill_change_notice"
    priority = 20
    checkpoints = {MessageMiddlewareCheckpoint.AFTER_TURN_CLOSED}

    def __init__(
        self,
        manager: SkillManager,
        notice_state: SkillNoticeState | None = None,
    ) -> None:
        self._manager = manager
        self._notice_state = notice_state or SkillNoticeState()

    async def process(
        self,
        ctx: MessageMiddlewareContext,
        tape,
    ):
        _ = ctx
        snapshot = self._manager.refresh_if_dirty()
        previous_digest = _last_notice_digest(tape)
        if previous_digest == snapshot.catalog_digest:
            return []
        request_digest = self._notice_state.last_model_request_digest(ctx.run_id)
        if request_digest == snapshot.catalog_digest:
            return []
        if previous_digest is None and request_digest is None:
            return []
        content = render_skill_change_notice_text(snapshot)
        self._notice_state.remember_model_request(ctx.run_id, snapshot.catalog_digest)
        return [
            InsertPatch(
                position=TapePosition(
                    kind="boundary",
                    boundary="conversation_end",
                ),
                items=[
                    InferenceMessage(
                        role=InferenceRole.USER,
                        content=content,
                    )
                ],
                durable=True,
                metadata={
                    "reason": "skill_catalog_changed",
                    "catalog_digest": snapshot.catalog_digest,
                    "snapshot_version": snapshot.version,
                },
            )
        ]


def _last_notice_digest(tape) -> str | None:
    for item in reversed(tape.model_visible()):
        content = item.content or ""
        match = _NOTICE_DIGEST_RE.search(content)
        if match:
            return match.group(1)
    return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "SkillChangeNoticeMiddleware",
    "SkillNoticeState",
    "SkillReminderMiddleware",
    "SkillRuntimeConfig",
    "SkillSystemSectionProvider",
]
