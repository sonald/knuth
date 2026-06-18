"use client";

import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { AgentConnection, ThreadSummary } from "../lib/agui";
import { fetchThreads } from "../lib/agui";
import {
  type DebugStreamFrame,
  type EventNamespace,
  type RawRuntimeEvent,
  NAMESPACE_COLOR,
  eventLinks,
  eventNamespace,
  resolveDebugConnection,
  streamDebugEvents,
  summarizeEvent,
} from "../lib/debug-events";

// A single row in the timeline, tagged with how it arrived so the UI can show
// replay vs live and dedupe durable events across reconnects.
type EventRow = {
  key: string;
  event: RawRuntimeEvent;
  live: boolean;
};

const ALL_NAMESPACES: EventNamespace[] = [
  "run",
  "step",
  "model",
  "tool",
  "approval",
  "message",
  "conversation",
  "verification",
  "other",
];

function relativeTime(createdAt: string | undefined, originMs: number): string {
  if (!createdAt) return "";
  const t = Date.parse(createdAt);
  if (Number.isNaN(t)) return "";
  const delta = (t - originMs) / 1000;
  return `+${delta.toFixed(2)}s`;
}

export function DebugEventViewer() {
  const [connection, setConnection] = useState<AgentConnection | null>(null);
  const [connError, setConnError] = useState<string>();
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string>();
  const [rows, setRows] = useState<EventRow[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string>();

  const [namespaces, setNamespaces] = useState<Set<EventNamespace>>(
    () => new Set(ALL_NAMESPACES),
  );
  const [showDurable, setShowDurable] = useState(true);
  const [showTransient, setShowTransient] = useState(true);
  const [grep, setGrep] = useState("");
  const [follow, setFollow] = useState(true);
  const [selectedKey, setSelectedKey] = useState<string>();
  const [highlightLink, setHighlightLink] = useState<{
    field: string;
    value: string;
  }>();

  const [isDesktop, setIsDesktop] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const seenSeqRef = useRef<Set<number>>(new Set());
  const transientCounter = useRef(0);
  const listRef = useRef<HTMLDivElement | null>(null);
  const followRef = useRef(true);
  followRef.current = follow;

  // Resolve the backend connection once (desktop sidecar or dev URL).
  useEffect(() => {
    setIsDesktop(typeof window !== "undefined" && Boolean(window.knuthDesktop));
    let cancelled = false;
    resolveDebugConnection()
      .then((conn) => {
        if (!cancelled) setConnection(conn);
      })
      .catch((err) => {
        if (!cancelled) setConnError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadThreads = useCallback(async () => {
    if (!connection) return;
    try {
      const list = await fetchThreads(connection);
      setThreads(list);
      setConnError(undefined);
    } catch (err) {
      setConnError(String(err));
    }
  }, [connection]);

  useEffect(() => {
    void loadThreads();
  }, [loadThreads]);

  // Subscribe to the raw event stream for the active thread. The stream itself
  // replays durable history first, then tails live events if the run is live.
  useEffect(() => {
    if (!connection || !activeThreadId) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    seenSeqRef.current = new Set();
    transientCounter.current = 0;
    setRows([]);
    setSelectedKey(undefined);
    setHighlightLink(undefined);
    setStreamError(undefined);
    setStreaming(true);

    const onFrame = (frame: DebugStreamFrame) => {
      if (frame.phase === "control") {
        if (frame.control === "error") {
          setStreamError(`${frame.code ?? "error"}: ${frame.message}`);
        }
        return;
      }
      const event = frame.event;
      const seq = typeof event.seq === "number" ? event.seq : null;
      let key: string;
      if (seq != null) {
        if (seenSeqRef.current.has(seq)) return; // dedupe durable replays
        seenSeqRef.current.add(seq);
        key = `d:${seq}`;
      } else {
        key = `t:${transientCounter.current++}`;
      }
      setRows((prev) => [...prev, { key, event, live: frame.phase === "live" }]);
    };

    streamDebugEvents(connection, activeThreadId, onFrame, controller.signal)
      .then(() => setStreaming(false))
      .catch((err) => {
        if (!controller.signal.aborted) {
          setStreamError(String(err));
        }
        setStreaming(false);
      });

    return () => controller.abort();
  }, [connection, activeThreadId]);

  // Auto-scroll to the newest row while following.
  useEffect(() => {
    if (!followRef.current) return;
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [rows]);

  const originMs = useMemo(() => {
    const first = rows.find((r) => r.event.created_at);
    return first?.event.created_at ? Date.parse(first.event.created_at) : Date.now();
  }, [rows]);

  const grepLower = grep.trim().toLowerCase();
  const visibleRows = useMemo(() => {
    return rows.filter((row) => {
      const { event } = row;
      const ns = eventNamespace(event.type);
      if (!namespaces.has(ns)) return false;
      const isTransient = event.durability === "transient";
      if (isTransient && !showTransient) return false;
      if (!isTransient && !showDurable) return false;
      if (grepLower) {
        const haystack = JSON.stringify(event).toLowerCase();
        if (!haystack.includes(grepLower)) return false;
      }
      return true;
    });
  }, [rows, namespaces, showDurable, showTransient, grepLower]);

  const selected = useMemo(
    () => rows.find((r) => r.key === selectedKey),
    [rows, selectedKey],
  );

  const toggleNamespace = (ns: EventNamespace) => {
    setNamespaces((prev) => {
      const next = new Set(prev);
      if (next.has(ns)) next.delete(ns);
      else next.add(ns);
      return next;
    });
  };

  const exportJsonl = () => {
    const text = visibleRows.map((r) => JSON.stringify(r.event)).join("\n");
    const blob = new Blob([text], { type: "application/x-ndjson" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${activeThreadId ?? "events"}.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const counts = useMemo(() => {
    let durable = 0;
    let transient = 0;
    for (const row of rows) {
      if (row.event.durability === "transient") transient += 1;
      else durable += 1;
    }
    return { durable, transient, total: rows.length };
  }, [rows]);

  return (
    <div className="dbg-root">
      <header className="dbg-topbar">
        <div className="dbg-title">
          <span className="dbg-dot" />
          Knuth · Event Viewer
        </div>
        <div className="dbg-conn">
          {connError ? (
            <span className="dbg-bad">{connError}</span>
          ) : (
            <span className="dbg-muted">{connection?.baseUrl ?? "resolving…"}</span>
          )}
          <button className="dbg-btn" onClick={() => void loadThreads()}>
            ↻ threads
          </button>
          {isDesktop ? (
            <button className="dbg-btn" onClick={() => window.close()}>
              ✕ close
            </button>
          ) : (
            <a className="dbg-btn" href="/">
              ← chat
            </a>
          )}
        </div>
      </header>

      <div className="dbg-body">
        <aside className="dbg-threads">
          <div className="dbg-pane-head">Threads</div>
          <div className="dbg-thread-list">
            {threads.length === 0 && (
              <div className="dbg-empty">No runs yet.</div>
            )}
            {threads.map((thread) => (
              <button
                key={thread.threadId}
                className={`dbg-thread${thread.threadId === activeThreadId ? " is-active" : ""}`}
                onClick={() => setActiveThreadId(thread.threadId)}
              >
                <div className="dbg-thread-top">
                  <span className={`dbg-status dbg-status-${thread.status}`}>
                    {thread.status}
                  </span>
                  <span className="dbg-seq">#{thread.lastSeq}</span>
                </div>
                <div className="dbg-thread-query">{thread.query || thread.threadId}</div>
                <div className="dbg-thread-id">{thread.threadId}</div>
              </button>
            ))}
          </div>
        </aside>

        <main className="dbg-center">
          <div className="dbg-toolbar">
            <label className="dbg-toggle">
              <input
                type="checkbox"
                checked={follow}
                onChange={(e) => setFollow(e.target.checked)}
              />
              follow
            </label>
            <span className="dbg-sep" />
            <div className="dbg-ns-filters">
              {ALL_NAMESPACES.map((ns) => (
                <button
                  key={ns}
                  className={`dbg-chip${namespaces.has(ns) ? " is-on" : ""}`}
                  style={{ "--chip": NAMESPACE_COLOR[ns] } as CSSProperties}
                  onClick={() => toggleNamespace(ns)}
                >
                  {ns}
                </button>
              ))}
            </div>
            <span className="dbg-sep" />
            <label className="dbg-toggle">
              <input
                type="checkbox"
                checked={showDurable}
                onChange={(e) => setShowDurable(e.target.checked)}
              />
              durable
            </label>
            <label className="dbg-toggle">
              <input
                type="checkbox"
                checked={showTransient}
                onChange={(e) => setShowTransient(e.target.checked)}
              />
              transient
            </label>
            <span className="dbg-sep" />
            <input
              className="dbg-grep"
              placeholder="grep JSON…"
              value={grep}
              onChange={(e) => setGrep(e.target.value)}
            />
            <span className="dbg-spacer" />
            <button className="dbg-btn" onClick={exportJsonl}>
              export .jsonl
            </button>
          </div>

          {highlightLink && (
            <div className="dbg-linkbar">
              linked by <code>{highlightLink.field}</code> ={" "}
              <code>{highlightLink.value}</code>
              <button className="dbg-btn dbg-btn-sm" onClick={() => setHighlightLink(undefined)}>
                clear
              </button>
            </div>
          )}

          <div className="dbg-list" ref={listRef}>
            <div className="dbg-list-head">
              <span className="dbg-col-seq">seq</span>
              <span className="dbg-col-time">t</span>
              <span className="dbg-col-type">type</span>
              <span className="dbg-col-sum">summary</span>
            </div>
            {visibleRows.map((row) => {
              const { event } = row;
              const ns = eventNamespace(event.type);
              const links = eventLinks(event);
              const isLinked =
                highlightLink != null &&
                links[highlightLink.field] === highlightLink.value;
              const isTransient = event.durability === "transient";
              return (
                <button
                  key={row.key}
                  className={`dbg-row${row.key === selectedKey ? " is-selected" : ""}${isLinked ? " is-linked" : ""}${isTransient ? " is-transient" : ""}`}
                  onClick={() => setSelectedKey(row.key)}
                >
                  <span className="dbg-col-seq">
                    {typeof event.seq === "number" ? event.seq : "·"}
                  </span>
                  <span className="dbg-col-time">
                    {relativeTime(event.created_at, originMs)}
                  </span>
                  <span
                    className="dbg-col-type"
                    style={{ color: NAMESPACE_COLOR[ns] }}
                  >
                    <span
                      className="dbg-durdot"
                      data-durable={isTransient ? "0" : "1"}
                    />
                    {event.type}
                  </span>
                  <span className="dbg-col-sum">{summarizeEvent(event)}</span>
                </button>
              );
            })}
            {streaming && <div className="dbg-streaming">● streaming…</div>}
            {streamError && <div className="dbg-bad dbg-pad">{streamError}</div>}
            {!activeThreadId && (
              <div className="dbg-empty dbg-pad">Select a thread to inspect its events.</div>
            )}
          </div>

          <div className="dbg-statusbar">
            <span>{counts.total} events</span>
            <span className="dbg-muted">
              {counts.durable} durable · {counts.transient} transient
            </span>
            <span className="dbg-spacer" />
            <span className="dbg-muted">{visibleRows.length} shown</span>
          </div>
        </main>

        <aside className="dbg-inspector">
          <div className="dbg-pane-head">Inspector</div>
          {selected ? (
            <div className="dbg-inspect-body">
              <div className="dbg-inspect-type">{selected.event.type}</div>
              <div className="dbg-inspect-links">
                {Object.entries(eventLinks(selected.event)).map(([field, value]) => (
                  <button
                    key={field}
                    className="dbg-linkchip"
                    onClick={() => setHighlightLink({ field, value })}
                    title={`Highlight events with ${field}=${value}`}
                  >
                    {field}: {value.slice(0, 10)}
                  </button>
                ))}
              </div>
              <div className="dbg-inspect-actions">
                <button
                  className="dbg-btn dbg-btn-sm"
                  onClick={() =>
                    navigator.clipboard?.writeText(
                      JSON.stringify(selected.event, null, 2),
                    )
                  }
                >
                  copy JSON
                </button>
              </div>
              <pre className="dbg-json">
                {JSON.stringify(selected.event, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="dbg-empty dbg-pad">Select an event.</div>
          )}
        </aside>
      </div>
    </div>
  );
}
