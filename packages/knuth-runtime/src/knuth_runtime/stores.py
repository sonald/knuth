from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import anyio

from knuth.core.events import (
    DurableRuntimeEventDraft,
    StoredRuntimeEvent,
    parse_stored_runtime_event_json,
    store_runtime_event,
)
from knuth.core.runs import AgentRun
from knuth.core.types import RunStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RunStore(Protocol):
    async def create(self, query: str, metadata: dict[str, Any] | None = None) -> AgentRun:
        ...

    async def get(self, run_id: str) -> AgentRun:
        ...

    async def set_status(self, run_id: str, status: RunStatus) -> AgentRun:
        ...

    async def list_runs(self, limit: int = 20) -> list[AgentRun]:
        ...


class EventStore(Protocol):
    async def append(
        self,
        run_id: str,
        event: DurableRuntimeEventDraft,
    ) -> StoredRuntimeEvent:
        ...

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        ...


class MemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}

    async def create(self, query: str, metadata: dict[str, Any] | None = None) -> AgentRun:
        now = utc_now()
        run = AgentRun(
            id=f"run_{uuid4().hex}",
            query=query,
            status=RunStatus.CREATED,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        self._runs[run.id] = run
        return run

    async def get(self, run_id: str) -> AgentRun:
        return self._runs[run_id]

    async def set_status(self, run_id: str, status: RunStatus) -> AgentRun:
        run = self._runs[run_id].model_copy(
            update={"status": status, "updated_at": utc_now()}
        )
        self._runs[run_id] = run
        return run

    async def list_runs(self, limit: int = 20) -> list[AgentRun]:
        runs = sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)
        return runs[:limit]


class MemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[str, list[StoredRuntimeEvent]] = {}

    async def append(
        self,
        run_id: str,
        event: DurableRuntimeEventDraft,
    ) -> StoredRuntimeEvent:
        events = self._events.setdefault(run_id, [])
        stored_event = store_runtime_event(
            run_id,
            len(events) + 1,
            event,
            event_id=f"evt_{uuid4().hex}",
            created_at=utc_now(),
        )
        events.append(stored_event)
        return stored_event

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        events = self._events.get(run_id, [])
        if after_seq is None:
            return list(events)
        return [event for event in events if event.seq > after_seq]


class SQLiteStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        import sqlite3

        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists runs (
                  id text primary key,
                  status text not null,
                  query text not null,
                  created_at text not null,
                  updated_at text not null,
                  data_json text not null
                );
                create table if not exists events (
                  id text primary key,
                  run_id text not null,
                  seq integer not null,
                  type text not null,
                  event_json text not null,
                  created_at text not null,
                  unique(run_id, seq)
                );
                create table if not exists approvals (
                  id text primary key,
                  run_id text not null,
                  status text not null,
                  data_json text not null,
                  created_at text not null,
                  resolved_at text
                );
                """
            )
            columns = {
                row[1]
                for row in conn.execute("pragma table_info(events)").fetchall()
            }
            legacy_columns = {"namespace", "name", "payload_json", "durability"}
            required_columns = {"id", "run_id", "seq", "type", "event_json", "created_at"}
            if columns & legacy_columns or not required_columns.issubset(columns):
                raise RuntimeError(
                    "breaking event schema: remove the legacy events table or use a new database"
                )

    async def create(self, query: str, metadata: dict[str, Any] | None = None) -> AgentRun:
        now = utc_now()
        run = AgentRun(
            id=f"run_{uuid4().hex}",
            query=query,
            status=RunStatus.CREATED,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        await anyio.to_thread.run_sync(self._insert_run, run)
        return run

    def _insert_run(self, run: AgentRun) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into runs values (?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.status.value,
                    run.query,
                    run.created_at,
                    run.updated_at,
                    run.model_dump_json(),
                ),
            )

    async def get(self, run_id: str) -> AgentRun:
        return await anyio.to_thread.run_sync(self._get_run, run_id)

    def _get_run(self, run_id: str) -> AgentRun:
        with self._connect() as conn:
            row = conn.execute("select data_json from runs where id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return AgentRun.model_validate_json(row[0])

    async def set_status(self, run_id: str, status: RunStatus) -> AgentRun:
        run = (await self.get(run_id)).model_copy(
            update={"status": status, "updated_at": utc_now()}
        )
        await anyio.to_thread.run_sync(self._update_run, run)
        return run

    def _update_run(self, run: AgentRun) -> None:
        with self._connect() as conn:
            conn.execute(
                "update runs set status = ?, updated_at = ?, data_json = ? where id = ?",
                (run.status.value, run.updated_at, run.model_dump_json(), run.id),
            )

    async def list_runs(self, limit: int = 20) -> list[AgentRun]:
        return await anyio.to_thread.run_sync(self._list_runs, limit)

    def _list_runs(self, limit: int) -> list[AgentRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "select data_json from runs order by created_at desc limit ?",
                (limit,),
            ).fetchall()
        return [AgentRun.model_validate_json(row[0]) for row in rows]

    async def append(
        self,
        run_id: str,
        event: DurableRuntimeEventDraft,
    ) -> StoredRuntimeEvent:
        return await anyio.to_thread.run_sync(
            self._append_event,
            run_id,
            event,
        )

    def _append_event(
        self,
        run_id: str,
        event: DurableRuntimeEventDraft,
    ) -> StoredRuntimeEvent:
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(seq), 0) + 1 from events where run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row[0])
            stored_event = store_runtime_event(
                run_id,
                seq,
                event,
                event_id=f"evt_{uuid4().hex}",
                created_at=utc_now(),
            )
            conn.execute(
                "insert into events (id, run_id, seq, type, event_json, created_at) values (?, ?, ?, ?, ?, ?)",
                (
                    stored_event.id,
                    stored_event.run_id,
                    stored_event.seq,
                    stored_event.type,
                    stored_event.model_dump_json(),
                    stored_event.created_at,
                ),
            )
        return stored_event

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[StoredRuntimeEvent]:
        return await anyio.to_thread.run_sync(self._list_events, run_id, after_seq)

    def _list_events(self, run_id: str, after_seq: int | None = None) -> list[StoredRuntimeEvent]:
        sql = "select event_json from events where run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if after_seq is not None:
            sql += " and seq > ?"
            params = (run_id, after_seq)
        sql += " order by seq"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [parse_stored_runtime_event_json(row[0]) for row in rows]
