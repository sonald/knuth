from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

import anyio

from knuth.core.artifacts import ArtifactSink, ArtifactSinkProvider, StoredArtifact

from knuth_runtime.redaction import RegexSecretRedactor

_SAFE_EXT = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.:-]+$")
_MANIFEST_NAME = "manifest.json"


class FilesystemArtifactStore(ArtifactSinkProvider):
    def __init__(
        self,
        root: Path | str,
        *,
        redactor: RegexSecretRedactor,
        ttl_days: float = 7.0,
    ) -> None:
        self.root = Path(root).expanduser()
        self.redactor = redactor
        self.ttl = timedelta(days=ttl_days)
        self._lock = anyio.Lock()

    def sink_for(self, run_id: str, tool_call_id: str) -> ArtifactSink:
        _safe_component(run_id, "run_id")
        _safe_component(tool_call_id, "tool_call_id")
        return _FilesystemArtifactSink(self, run_id, tool_call_id)

    async def put(
        self,
        run_id: str,
        content: str,
        *,
        kind: str,
        ext: str | None = None,
        tool_call_id: str | None = None,
    ) -> StoredArtifact:
        safe_ext = _safe_ext(ext)
        _safe_component(run_id, "run_id")
        redacted = self.redactor.redact_text(content)
        payload = redacted.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = f"art_{uuid4().hex}"
        rel_path = f"{artifact_id}{safe_ext}"
        created_at = datetime.now(UTC).isoformat()

        async with self._lock:
            run_dir = self._run_dir(run_id)
            path = run_dir / rel_path
            await anyio.to_thread.run_sync(
                _atomic_write_bytes,
                path,
                payload,
            )
            manifest = await anyio.to_thread.run_sync(self._load_manifest_sync, run_id)
            manifest["artifacts"][artifact_id] = {
                "rel_path": rel_path,
                "ext": safe_ext,
                "kind": kind,
                "sha256": digest,
                "bytes": len(payload),
                "state": "pending",
                "created_at": created_at,
                "tool_call_id": tool_call_id,
            }
            await anyio.to_thread.run_sync(
                self._write_manifest_sync,
                run_id,
                manifest,
            )
        return StoredArtifact(
            id=artifact_id,
            path=str(path),
            kind=kind,
            sha256=digest,
            bytes=len(payload),
        )

    async def mark_committed(self, run_id: str, artifact_ids: list[str]) -> None:
        if not artifact_ids:
            return
        _safe_component(run_id, "run_id")
        async with self._lock:
            manifest = await anyio.to_thread.run_sync(self._load_manifest_sync, run_id)
            entries = manifest["artifacts"]
            changed = False
            for artifact_id in artifact_ids:
                entry = entries.get(artifact_id)
                # A tool may report an id it never actually stored; there is
                # nothing to commit, so skip rather than raise after the
                # referencing event is already durable.
                if entry is None or entry.get("state") == "committed":
                    continue
                entry["state"] = "committed"
                changed = True
            if changed:
                await anyio.to_thread.run_sync(self._write_manifest_sync, run_id, manifest)

    def path_for(self, run_id: str, artifact_id: str) -> Path:
        _safe_component(run_id, "run_id")
        _safe_component(artifact_id, "artifact_id")
        manifest = self._load_manifest_sync(run_id)
        entry = manifest["artifacts"].get(artifact_id)
        if entry is None:
            raise KeyError(artifact_id)
        path = self._resolve_entry_path(run_id, entry)
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    async def read_text(self, run_id: str, artifact_id: str) -> str:
        path = self.path_for(run_id, artifact_id)
        return await anyio.Path(path).read_text(encoding="utf-8")

    async def gc(self) -> None:
        cutoff = datetime.now(UTC) - self.ttl
        async with self._lock:
            await anyio.to_thread.run_sync(self._gc_sync, cutoff)

    async def reclaim_run(self, run_id: str) -> None:
        _safe_component(run_id, "run_id")
        async with self._lock:
            await anyio.to_thread.run_sync(shutil.rmtree, self._run_dir(run_id), True)

    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def _manifest_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / _MANIFEST_NAME

    def _load_manifest_sync(self, run_id: str) -> dict:
        manifest_path = self._manifest_path(run_id)
        if not manifest_path.exists():
            return {"version": 1, "artifacts": {}}
        with manifest_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if data.get("version") != 1 or not isinstance(data.get("artifacts"), dict):
            raise ValueError(f"invalid artifact manifest for run {run_id}")
        return data

    def _write_manifest_sync(self, run_id: str, manifest: dict) -> None:
        _atomic_write_bytes(
            self._manifest_path(run_id),
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(
                "utf-8"
            ),
        )

    def _resolve_entry_path(self, run_id: str, entry: dict) -> Path:
        rel_path = entry.get("rel_path")
        if not isinstance(rel_path, str) or "/" in rel_path or ".." in rel_path:
            raise ValueError(f"invalid artifact path in manifest for run {run_id}")
        return self._run_dir(run_id) / rel_path

    def _gc_sync(self, cutoff: datetime) -> None:
        if not self.root.exists():
            return
        for run_dir in self.root.iterdir():
            if not run_dir.is_dir():
                continue
            run_id = run_dir.name
            try:
                _safe_component(run_id, "run_id")
                manifest = self._load_manifest_sync(run_id)
            except (OSError, ValueError):
                continue
            entries = manifest["artifacts"]
            changed = False
            for artifact_id, entry in list(entries.items()):
                if entry.get("state") != "pending":
                    continue
                created_at = _parse_timestamp(entry.get("created_at"))
                path = self._resolve_entry_path(run_id, entry)
                if created_at <= cutoff or not path.exists():
                    path.unlink(missing_ok=True)
                    del entries[artifact_id]
                    changed = True
            if changed:
                self._write_manifest_sync(run_id, manifest)


class _FilesystemArtifactSink:
    def __init__(
        self,
        store: FilesystemArtifactStore,
        run_id: str,
        tool_call_id: str,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._tool_call_id = tool_call_id

    async def put(
        self,
        content: str,
        *,
        kind: str,
        ext: str | None = None,
    ) -> StoredArtifact:
        return await self._store.put(
            self._run_id,
            content,
            kind=kind,
            ext=ext,
            tool_call_id=self._tool_call_id,
        )


def _safe_ext(ext: str | None) -> str:
    if ext is None or ext == "":
        return ""
    if ".." in ext or not _SAFE_EXT.fullmatch(ext):
        raise ValueError(f"unsafe artifact extension: {ext}")
    return ext


def _safe_component(value: str, label: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or ".." in value
        or not _SAFE_COMPONENT.fullmatch(value)
    ):
        raise ValueError(f"unsafe artifact {label}: {value}")


def _parse_timestamp(raw: object) -> datetime:
    if not isinstance(raw, str):
        return datetime.fromtimestamp(0, UTC)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with tmp.open("wb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


__all__ = ["FilesystemArtifactStore"]
