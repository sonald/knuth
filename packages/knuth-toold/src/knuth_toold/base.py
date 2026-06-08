from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from knuth.core.tools import ToolResult, ToolResultStatus
from knuth.core.types import KnuthModel


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
