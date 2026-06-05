from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from knuth.core.types import ErrorInfo, KnuthModel


class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolEffect(StrEnum):
    PURE = "pure"
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    DANGEROUS = "dangerous"


class ToolManifest(KnuthModel):
    name: str
    description: str
    parameters: dict[str, Any]
    parallelable: bool = False
    cacheable: bool = False
    risk: ToolRisk = ToolRisk.LOW
    effect: ToolEffect = ToolEffect.READ
    provider: str = "builtin"

    def to_func_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

class ToolContext(KnuthModel):
    run_id: str
    tool_call_id: str
    workspace_uri: str | None = None

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace_uri or ".").resolve()


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ToolResult(KnuthModel):
    status: ToolResultStatus
    content: str | None = None
    data: Any = None
    error: ErrorInfo | None = None
    artifacts: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS

    def to_observation_text(self) -> str:
        if self.status == ToolResultStatus.SUCCESS:
            return self.content if self.content is not None else repr(self.data)
        return f"Tool error: {self.error.message if self.error else 'unknown error'}"

    @classmethod
    def success(cls, content: str | None = None, data: Any = None) -> "ToolResult":
        return cls(status=ToolResultStatus.SUCCESS, content=content, data=data)

    @classmethod
    def from_error(
        cls, code: str, message: str, retryable: bool = False
    ) -> "ToolResult":
        return cls(
            status=ToolResultStatus.ERROR,
            content="",
            error=ErrorInfo(code=code, message=message, retryable=retryable),
        )


class ToolBase(KnuthModel, ABC):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str
    parameters: dict[str, Any]
    parallelable: bool = False
    cacheable: bool = False
    risk: ToolRisk = ToolRisk.LOW
    effect: ToolEffect = ToolEffect.READ
    default_workspace_uri: str | None = None

    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            parallelable=self.parallelable,
            cacheable=self.cacheable,
            risk=self.risk,
            effect=self.effect,
            provider="builtin",
        )

    def to_func_spec(self) -> dict[str, Any]:
        return self.manifest().to_func_spec()

    @abstractmethod
    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        ...
