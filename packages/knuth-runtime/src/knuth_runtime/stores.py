from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import anyio

from knuth.core.events import RuntimeEvent
from knuth.core.runs import AgentRun
from knuth.core.types import EventDurability, RunStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RunStore(Protocol):
    async def create(self, query: str, metadata: dict[str, Any] | None = None) -> AgentRun:
        ...

    async def get(self, run_id: str) -> AgentRun:
        ...

    async def set_status(self, run_id: str, status: RunStatus) -> AgentRun:
        ...


class EventStore(Protocol):
    async def append(
        self,
        run_id: str,
        namespace: str,
        name: str,
        payload: dict[str, Any] | None = None,
        durability: EventDurability = EventDurability.DURABLE,
    ) -> RuntimeEvent:
        ...

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[RuntimeEvent]:
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


class MemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[str, list[RuntimeEvent]] = {}

    async def append(
        self,
        run_id: str,
        namespace: str,
        name: str,
        payload: dict[str, Any] | None = None,
        durability: EventDurability = EventDurability.DURABLE,
    ) -> RuntimeEvent:
        events = self._events.setdefault(run_id, [])
        event = RuntimeEvent(
            id=f"evt_{uuid4().hex}",
            run_id=run_id,
            seq=len(events) + 1,
            namespace=namespace,
            name=name,
            type=f"{namespace}.{name}",
            payload=payload or {},
            durability=durability,
            created_at=utc_now(),
        )
        events.append(event)
        return event

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[RuntimeEvent]:
        events = self._events.get(run_id, [])
        if after_seq is None:
            return list(events)
        return [event for event in events if event.seq > after_seq]


class JsonStore:
    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        if not self.state_path.exists():
            self._write_state({"runs": {}, "events": {}})

    def _read_state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, Any]) -> None:
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

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
        await anyio.to_thread.run_sync(self._save_run, run)
        return run

    def _save_run(self, run: AgentRun) -> None:
        state = self._read_state()
        state.setdefault("runs", {})[run.id] = run.model_dump()
        state.setdefault("events", {}).setdefault(run.id, [])
        self._write_state(state)

    async def get(self, run_id: str) -> AgentRun:
        return await anyio.to_thread.run_sync(self._get_run, run_id)

    def _get_run(self, run_id: str) -> AgentRun:
        state = self._read_state()
        return AgentRun.model_validate(state["runs"][run_id])

    async def set_status(self, run_id: str, status: RunStatus) -> AgentRun:
        run = (await self.get(run_id)).model_copy(
            update={"status": status, "updated_at": utc_now()}
        )
        await anyio.to_thread.run_sync(self._save_run, run)
        return run

    async def append(
        self,
        run_id: str,
        namespace: str,
        name: str,
        payload: dict[str, Any] | None = None,
        durability: EventDurability = EventDurability.DURABLE,
    ) -> RuntimeEvent:
        return await anyio.to_thread.run_sync(
            self._append_event,
            run_id,
            namespace,
            name,
            payload or {},
            durability,
        )

    def _append_event(
        self,
        run_id: str,
        namespace: str,
        name: str,
        payload: dict[str, Any],
        durability: EventDurability,
    ) -> RuntimeEvent:
        state = self._read_state()
        events = state.setdefault("events", {}).setdefault(run_id, [])
        event = RuntimeEvent(
            id=f"evt_{uuid4().hex}",
            run_id=run_id,
            seq=len(events) + 1,
            namespace=namespace,
            name=name,
            type=f"{namespace}.{name}",
            payload=payload,
            durability=durability,
            created_at=utc_now(),
        )
        events.append(event.model_dump())
        self._write_state(state)
        return event

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[RuntimeEvent]:
        return await anyio.to_thread.run_sync(self._list_events, run_id, after_seq)

    def _list_events(self, run_id: str, after_seq: int | None = None) -> list[RuntimeEvent]:
        state = self._read_state()
        events = [
            RuntimeEvent.model_validate(item)
            for item in state.setdefault("events", {}).get(run_id, [])
        ]
        if after_seq is None:
            return events
        return [event for event in events if event.seq > after_seq]


class SQLiteStore(MemoryRunStore, MemoryEventStore):
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
                  namespace text not null,
                  name text not null,
                  type text not null,
                  payload_json text not null,
                  durability text not null,
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

    async def append(
        self,
        run_id: str,
        namespace: str,
        name: str,
        payload: dict[str, Any] | None = None,
        durability: EventDurability = EventDurability.DURABLE,
    ) -> RuntimeEvent:
        return await anyio.to_thread.run_sync(
            self._append_event,
            run_id,
            namespace,
            name,
            payload or {},
            durability,
        )

    def _append_event(
        self,
        run_id: str,
        namespace: str,
        name: str,
        payload: dict[str, Any],
        durability: EventDurability,
    ) -> RuntimeEvent:
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(seq), 0) + 1 from events where run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row[0])
            event = RuntimeEvent(
                id=f"evt_{uuid4().hex}",
                run_id=run_id,
                seq=seq,
                namespace=namespace,
                name=name,
                type=f"{namespace}.{name}",
                payload=payload,
                durability=durability,
                created_at=utc_now(),
            )
            conn.execute(
                "insert into events (id, run_id, seq, namespace, name, type, payload_json, durability, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.run_id,
                    event.seq,
                    event.namespace,
                    event.name,
                    event.type,
                    json.dumps(event.payload),
                    event.durability.value,
                    event.created_at,
                ),
            )
        return event

    async def list_events(
        self, run_id: str, after_seq: int | None = None
    ) -> list[RuntimeEvent]:
        return await anyio.to_thread.run_sync(self._list_events, run_id, after_seq)

    def _list_events(self, run_id: str, after_seq: int | None = None) -> list[RuntimeEvent]:
        sql = "select id, run_id, seq, namespace, name, type, payload_json, durability, created_at from events where run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if after_seq is not None:
            sql += " and seq > ?"
            params = (run_id, after_seq)
        sql += " order by seq"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            RuntimeEvent(
                id=row[0],
                run_id=row[1],
                seq=row[2],
                namespace=row[3],
                name=row[4],
                type=row[5],
                payload=json.loads(row[6]),
                durability=EventDurability(row[7]),
                created_at=row[8],
            )
            for row in rows
        ]
