from __future__ import annotations

from typing import Protocol, runtime_checkable

from knuth.core.types import KnuthModel


class StoredArtifact(KnuthModel):
    id: str
    path: str
    kind: str
    sha256: str
    bytes: int


@runtime_checkable
class ArtifactSink(Protocol):
    async def put(
        self,
        content: str,
        *,
        kind: str,
        ext: str | None = None,
    ) -> StoredArtifact:
        ...


@runtime_checkable
class ArtifactSinkProvider(Protocol):
    def sink_for(self, run_id: str, tool_call_id: str) -> ArtifactSink:
        ...


__all__ = [
    "ArtifactSink",
    "ArtifactSinkProvider",
    "StoredArtifact",
]
