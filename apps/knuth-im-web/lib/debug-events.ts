// Raw RuntimeEvent debug channel client.
//
// The chat UI consumes the translated /agent stream (lib/agui.ts). The debug
// viewer instead talks to the untranslated endpoints added in knuth-agui:
// GET /threads/{id}/events for full-fidelity history and
// GET /threads/{id}/events/stream for a raw replay + live tail. We read the
// stream with a bare fetch reader rather than @ag-ui/client because these are
// raw Knuth frames, not AG-UI protocol events.

import {
  DEFAULT_AGENT_URL,
  type AgentConnection,
  type AgentEndpoint,
} from "./agui";

export type RawRuntimeEvent = {
  type: string;
  durability?: "durable" | "transient";
  seq?: number;
  id?: string;
  run_id?: string;
  created_at?: string;
  [key: string]: unknown;
};

// One frame off the raw SSE stream. `phase` distinguishes replayed history
// from live events and from control sentinels (replay_complete / error).
export type DebugStreamFrame =
  | { phase: "replay" | "live"; event: RawRuntimeEvent }
  | {
      phase: "control";
      control: "replay_complete";
      lastSeq: number;
      live: boolean;
    }
  | { phase: "control"; control: "error"; message: string; code?: string };

export type DebugEventsResponse = {
  runId: string;
  events: RawRuntimeEvent[];
  lastSeq: number | null;
};

function normalize(endpoint: AgentEndpoint): AgentConnection {
  if (typeof endpoint === "string") {
    return { baseUrl: endpoint, headers: {} };
  }
  return { ...endpoint, headers: endpoint.headers ?? {} };
}

// Mirror the chat app's connection resolution without coupling to its 2k-line
// component: prefer the desktop sidecar (Electron bridge), else the dev URL.
export async function resolveDebugConnection(): Promise<AgentConnection> {
  if (typeof window !== "undefined" && window.knuthDesktop?.backend) {
    try {
      const backend = await window.knuthDesktop.backend();
      if (backend?.baseUrl) {
        return { ...backend, headers: backend.headers ?? {} };
      }
    } catch {
      // Fall through to the default URL.
    }
  }
  return { baseUrl: DEFAULT_AGENT_URL, headers: {} };
}

export async function fetchDebugEvents(
  endpoint: AgentEndpoint,
  threadId: string,
  afterSeq?: number,
): Promise<DebugEventsResponse> {
  const connection = normalize(endpoint);
  const query = afterSeq != null ? `?after_seq=${afterSeq}` : "";
  const response = await fetch(
    `${connection.baseUrl}/threads/${threadId}/events${query}`,
    { cache: "no-store", headers: connection.headers },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as DebugEventsResponse;
}

// Read the raw SSE event stream, invoking onFrame per decoded frame. Resolves
// when the server closes the stream (finished run) and rejects on abort.
export async function streamDebugEvents(
  endpoint: AgentEndpoint,
  threadId: string,
  onFrame: (frame: DebugStreamFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  const connection = normalize(endpoint);
  const response = await fetch(
    `${connection.baseUrl}/threads/${threadId}/events/stream`,
    { headers: connection.headers, signal },
  );
  if (!response.ok || !response.body) {
    throw new Error(response.ok ? "no response body" : await response.text());
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line.
      let split = buffer.indexOf("\n\n");
      while (split !== -1) {
        const block = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const line = block.trim();
        if (line.startsWith("data:")) {
          const json = line.slice("data:".length).trim();
          if (json) {
            try {
              onFrame(JSON.parse(json) as DebugStreamFrame);
            } catch {
              // Skip a malformed frame rather than killing the stream.
            }
          }
        }
        split = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// --- presentation helpers ---------------------------------------------------

export type EventNamespace =
  | "run"
  | "step"
  | "model"
  | "tool"
  | "approval"
  | "message"
  | "conversation"
  | "verification"
  | "other";

const KNOWN_NAMESPACES = new Set<EventNamespace>([
  "run",
  "step",
  "model",
  "tool",
  "approval",
  "message",
  "conversation",
  "verification",
]);

export function eventNamespace(type: string): EventNamespace {
  const head = type.split(".", 1)[0] as EventNamespace;
  return KNOWN_NAMESPACES.has(head) ? head : "other";
}

// Color is driven from the app's existing CSS palette so the viewer matches
// light/dark themes. Each namespace maps to a theme variable.
export const NAMESPACE_COLOR: Record<EventNamespace, string> = {
  run: "var(--accent-ink)",
  step: "var(--blue)",
  model: "var(--blue)",
  tool: "var(--green)",
  approval: "var(--amber)",
  message: "var(--ink-faint)",
  conversation: "var(--ink-soft)",
  verification: "var(--red)",
  other: "var(--ink-faint)",
};

function str(value: unknown, max = 80): string {
  if (value == null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function shortHash(value: unknown): string {
  return typeof value === "string" ? value.slice(0, 6) : "";
}

// A dense one-liner per event type for the timeline's summary column. Falls
// back to a compact dump of the event's own (non-envelope) fields so unknown
// or new event types still say something useful.
export function summarizeEvent(event: RawRuntimeEvent): string {
  const e = event as Record<string, unknown>;
  switch (event.type) {
    case "run.created":
      return str(e.query, 100);
    case "user.message":
      return str(e.content, 100);
    case "run.resumed":
      return `cause=${e.cause}`;
    case "run.paused":
      return `${e.reason} · src=${e.source}`;
    case "run.interrupted":
      return `reason=${e.reason} phase=${e.active_phase}`;
    case "run.cancelled":
      return `${e.reason} · src=${e.source}`;
    case "run.failed":
      return str((e.error as Record<string, unknown>)?.message ?? e.error, 100);
    case "run.succeeded":
      return `${e.turns} turns · ${str(e.answer, 80)}`;
    case "conversation.notice":
      return `[${e.kind}] ${str(e.content, 80)}`;
    case "step.started": {
      const snap = (e.snapshot ?? {}) as Record<string, unknown>;
      return `${e.step_id} · msgs=${snap.message_count} tools=${snap.tool_count} · ctx ${shortHash(snap.messages_hash)}`;
    }
    case "model.completed": {
      const calls = (e.tool_calls as unknown[]) ?? [];
      const usage = (e.usage ?? {}) as Record<string, unknown>;
      const tok =
        usage.input_tokens != null || usage.output_tokens != null
          ? ` · in ${usage.input_tokens ?? "?"}/out ${usage.output_tokens ?? "?"}`
          : "";
      const finish = e.finish_reason ? `finish=${e.finish_reason}` : "completed";
      return `${finish} · ${calls.length} call(s)${tok}`;
    }
    case "model.failed":
      return str((e.error as Record<string, unknown>)?.message ?? e.error, 100);
    case "model.aborted":
      return `reason=${e.reason}`;
    case "model.content.delta":
      return str(e.delta, 100);
    case "model.reasoning.delta":
      return str(e.delta, 100);
    case "model.reasoning.completed":
      return "reasoning end";
    case "model.tool_call.started":
      return `#${e.index} ${str(e.tool_call_id)}`;
    case "model.tool_call.delta":
      return `#${e.index} ${str(e.name_delta)}${str(e.arguments_json_delta)}`;
    case "model.tool_call.completed": {
      const call = (e.tool_call ?? {}) as Record<string, unknown>;
      return `${call.name ?? ""} ${shortHash(call.tool_call_id)}`;
    }
    case "tool.batch_planned": {
      const calls = (e.calls as Array<Record<string, unknown>>) ?? [];
      const names = calls.map((c) => c.name).join(", ");
      return `${e.batch_id} · ${calls.length} call(s): ${str(names, 70)}`;
    }
    case "tool.proposed":
      return `${e.decision} · effect=${e.effect} risk=${e.risk}`;
    case "tool.invocation_started":
      return `${str(e.tool_call_id)} · attempt ${e.attempt}`;
    case "tool.invocation_awaiting_external_result":
      return `${e.tool_name} ${str(e.tool_call_id)}`;
    case "tool.invocation_completed":
      return `${e.tool_name} → ${e.outcome} · ${str(e.observation, 60)}`;
    case "tool.invocation_marked_unknown":
      return `${str(e.tool_call_id)} · ${e.reason}`;
    case "tool.batch_closed":
      return `${e.batch_id}`;
    case "approval.requested":
      return `risk=${e.risk} · ${str(e.title, 60)} · ${str(e.reason, 40)}`;
    case "approval.resolved":
      return `${e.resolution}${e.resolved_by ? ` by ${e.resolved_by}` : ""}`;
    case "message.rewrite_anchor":
      return `[${e.middleware}] ${e.operation} (${e.kind})`;
    case "message.rewrite_message": {
      const msg = (e.message ?? {}) as Record<string, unknown>;
      return `${msg.role ?? "?"} · ${str(msg.content, 70)}`;
    }
    case "verification.failed":
      return `${str(e.reason, 50)} · ${str(e.feedback, 50)}`;
    case "run.invocation.started":
      return `mode=${e.mode}`;
    case "run.invocation.ended":
      return `mode=${e.mode} · status=${e.status ?? "?"}`;
    default: {
      const omit = new Set([
        "type",
        "durability",
        "seq",
        "id",
        "run_id",
        "created_at",
      ]);
      const rest = Object.fromEntries(
        Object.entries(e).filter(([k]) => !omit.has(k)),
      );
      return str(rest, 100);
    }
  }
}

// Identity fields a row exposes for cross-linking related events.
export function eventLinks(event: RawRuntimeEvent): Record<string, string> {
  const e = event as Record<string, unknown>;
  const links: Record<string, string> = {};
  for (const key of [
    "step_id",
    "batch_id",
    "tool_call_id",
    "approval_id",
    "rewrite_id",
  ]) {
    const value = e[key];
    if (typeof value === "string" && value) {
      links[key] = value;
    }
  }
  return links;
}
