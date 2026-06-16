from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KnuthModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    schema_version: str = "v0"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_TOOL_RESULT = "waiting_tool_result"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"


class EventDurability(StrEnum):
    TRANSIENT = "transient"
    DURABLE = "durable"


class ErrorInfo(KnuthModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
