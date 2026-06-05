from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
from pydantic import Field

from knuth.core.types import KnuthModel
from knuth_runtime.stores import utc_now


class Artifact(KnuthModel):
    id: str
    run_id: str
    kind: str
    uri: str
    title: str | None = None
    created_at: str


class MemoryArtifactStore:
    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._content: dict[str, str] = {}

    async def put_text(
        self,
        run_id: str,
        kind: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        artifact = Artifact(
            id=f"artifact_{uuid4().hex}",
            run_id=run_id,
            kind=kind,
            title=title,
            uri=f"memory://{run_id}/{title}",
            created_at=utc_now(),
            metadata=metadata or {},
        )
        self._artifacts[artifact.id] = artifact
        self._content[artifact.id] = content
        return artifact

    async def get_text(self, artifact_id: str) -> str:
        return self._content[artifact_id]


class FileArtifactStore(MemoryArtifactStore):
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._artifacts: dict[str, Artifact] = {}

    async def put_text(
        self,
        run_id: str,
        kind: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        artifact_id = f"artifact_{uuid4().hex}"
        run_dir = self.root / run_id
        await anyio.Path(run_dir).mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{artifact_id}.txt"
        async with await anyio.open_file(path, "w", encoding="utf-8") as file:
            await file.write(content)
        artifact = Artifact(
            id=artifact_id,
            run_id=run_id,
            kind=kind,
            title=title,
            uri=str(path),
            created_at=utc_now(),
            metadata=metadata or {},
        )
        self._artifacts[artifact.id] = artifact
        return artifact

    async def get_text(self, artifact_id: str) -> str:
        artifact = self._artifacts[artifact_id]
        async with await anyio.open_file(Path(artifact.uri), encoding="utf-8") as file:
            return await file.read()
