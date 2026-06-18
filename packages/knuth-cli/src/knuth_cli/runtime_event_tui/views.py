from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from knuth_cli.runtime_event_tui.models import ObservedEventRow, RunSnapshot


def event_row_label(row: ObservedEventRow) -> str:
    source = "L" if row.source == "live" else "D"
    order = (
        f"{row.receive_index:03d}"
        if row.receive_index is not None
        else f"{row.durable_seq:03d}" if row.durable_seq is not None else "---"
    )
    return f"{source} {order} {row.durability} {row.event_type}"


def event_detail_text(row: ObservedEventRow | None) -> str:
    if row is None:
        return "No event selected."
    data = row.event.model_dump(mode="json", exclude_none=True)
    return json.dumps(data, indent=2, ensure_ascii=False)


def event_matches_filter(row: ObservedEventRow, query: str | None) -> bool:
    value = (query or "").strip().lower()
    if not value or value == "all":
        return True
    if value in {"durable", "transient"}:
        return row.durability == value
    event_type = row.event_type.lower()
    if value.endswith("*"):
        return event_type.startswith(value[:-1])
    return event_type.startswith(value)


def dedupe_event_rows(rows: Iterable[ObservedEventRow]) -> list[ObservedEventRow]:
    seen_durable: set[tuple[str, int] | tuple[str, str]] = set()
    result: list[ObservedEventRow] = []
    for row in rows:
        key = row.durable_key
        if key is not None:
            if key in seen_durable:
                continue
            seen_durable.add(key)
        result.append(row)
    return result


def latest_system_preamble(rows: Sequence[ObservedEventRow]) -> str | None:
    for row in reversed(rows):
        if row.event_type == "context.system_preamble.built":
            return getattr(row.event, "content", None)
    return None


def run_snapshot_text(snapshot: RunSnapshot | None) -> str:
    if snapshot is None:
        return "No run loaded."
    parts = [
        f"run_id: {snapshot.run_id or '-'}",
        f"status: {snapshot.status or '-'}",
        "",
        "pending approvals:",
    ]
    if snapshot.approvals:
        for approval in snapshot.approvals:
            status = f" [{approval.status}]" if approval.status else ""
            parts.append(f"- {approval.approval_id}{status} {approval.title}")
    else:
        parts.append("- none")
    parts.extend(["", "latest system preamble:"])
    parts.append(snapshot.latest_system_preamble or "<none>")
    parts.extend(["", f"raw ledger messages: {len(snapshot.messages)}"])
    parts.append(f"model context messages: {len(snapshot.model_context_messages)}")
    parts.append(f"rewrite audit records: {len(snapshot.rewrite_audit)}")
    if snapshot.listener_stats:
        parts.extend(["", "listener stats:"])
        for name, value in snapshot.listener_stats.items():
            parts.append(f"- {name}: {value}")
    if snapshot.error:
        parts.extend(["", f"error: {snapshot.error}"])
    return "\n".join(parts)


def json_text(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)
