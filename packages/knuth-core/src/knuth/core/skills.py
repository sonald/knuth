from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import field_validator

from knuth.core.types import KnuthModel

_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,63}$")


class SkillSource(StrEnum):
    PROJECT = "project"
    USER = "user"
    BUILTIN = "builtin"
    HOST = "host"


class SkillMetadata(KnuthModel):
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _SKILL_NAME_RE.match(value):
            raise ValueError(
                "skill name must be 1-64 lowercase letters, digits, or single "
                "hyphens, and cannot start/end with a hyphen"
            )
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("skill description must be non-empty")
        if len(value) > 1024:
            raise ValueError("skill description must be at most 1024 characters")
        return value

    @field_validator("compatibility")
    @classmethod
    def _validate_compatibility(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 500:
            raise ValueError("skill compatibility must be at most 500 characters")
        return value


class SkillInfo(KnuthModel):
    metadata: SkillMetadata
    source: SkillSource
    file_path: str


__all__ = ["SkillInfo", "SkillMetadata", "SkillSource"]
