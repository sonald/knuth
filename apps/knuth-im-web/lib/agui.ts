import { HttpAgent, type BaseEvent, type RunAgentInput } from "@ag-ui/client";

export const DEFAULT_AGENT_URL =
  process.env.NEXT_PUBLIC_KNUTH_AGUI_URL ?? "http://127.0.0.1:8000";

export type AgentConnection = {
  baseUrl: string;
  headers?: Record<string, string>;
  status?: string;
  mode?: string;
  workspace?: string;
  settings?: {
    modelBaseUrl: string;
    model: string;
    timeout: number;
    workspace: string;
    dbPath: string;
    hasApiKey: boolean;
    apiKeySource: "stored" | "environment" | null;
    secretStorage?: "local-file" | null;
    missing: string[];
    ready: boolean;
  };
  error?: string;
};

export type AgentEndpoint = string | AgentConnection;

export type ThreadSummary = {
  threadId: string;
  runId: string;
  status: string;
  query: string;
  createdAt: string;
  updatedAt: string;
  steps: number;
  lastSeq: number;
};

export type PendingApproval = {
  approvalId: string;
  runId: string;
  toolCallId: string;
  status: string;
  title: string;
  reason: string;
  risk: string;
  preview?: Record<string, unknown>;
  createdAt?: string;
};

export type AGUIEvent = {
  type: string;
  [key: string]: unknown;
};

export type WireMessage = {
  id?: string;
  role: "system" | "developer" | "user" | "assistant" | "tool";
  content?: unknown;
  toolCallId?: string;
  toolCalls?: Array<{
    id: string;
    function?: {
      name: string;
      arguments: string;
    };
  }>;
};

export type ClientToolSpec = {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
};

export type ToolResultInput = {
  runId: string;
  toolCallId: string;
  outcome?: "succeeded" | "failed";
  result?: unknown;
  error?: unknown;
};

function normalizeEndpoint(endpoint: AgentEndpoint): AgentConnection {
  if (typeof endpoint === "string") {
    return { baseUrl: endpoint, headers: {} };
  }
  return { ...endpoint, headers: endpoint.headers ?? {} };
}

function jsonHeaders(endpoint: AgentConnection): Record<string, string> {
  return { ...endpoint.headers, "content-type": "application/json" };
}

export async function fetchThreads(endpoint: AgentEndpoint): Promise<ThreadSummary[]> {
  const connection = normalizeEndpoint(endpoint);
  const response = await fetch(`${connection.baseUrl}/threads`, {
    cache: "no-store",
    headers: connection.headers,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const data = (await response.json()) as { threads?: ThreadSummary[] };
  return data.threads ?? [];
}

export async function fetchHistory(
  endpoint: AgentEndpoint,
  threadId: string,
): Promise<WireMessage[]> {
  const connection = normalizeEndpoint(endpoint);
  const response = await fetch(`${connection.baseUrl}/threads/${threadId}/history`, {
    cache: "no-store",
    headers: connection.headers,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const data = (await response.json()) as { messages?: WireMessage[] };
  return data.messages ?? [];
}

export async function fetchPendingApprovals(
  endpoint: AgentEndpoint,
  threadId: string,
): Promise<PendingApproval[]> {
  const connection = normalizeEndpoint(endpoint);
  const response = await fetch(`${connection.baseUrl}/threads/${threadId}/approvals`, {
    cache: "no-store",
    headers: connection.headers,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const data = (await response.json()) as { approvals?: PendingApproval[] };
  return data.approvals ?? [];
}

export async function pauseRun(endpoint: AgentEndpoint, runId: string): Promise<void> {
  const connection = normalizeEndpoint(endpoint);
  const response = await fetch(`${connection.baseUrl}/pause`, {
    method: "POST",
    headers: jsonHeaders(connection),
    body: JSON.stringify({ runId }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

// UI stop: interrupt active model/tool work via the live manager. Unlike
// pauseRun (a resumable runtime pause), this discards in-flight work so the run
// resolves to INTERRUPTED rather than a replayable PAUSED — matching ADR-007's
// "UI stop -> /stop -> live interrupt" semantics.
export async function stopRun(endpoint: AgentEndpoint, runId: string): Promise<void> {
  const connection = normalizeEndpoint(endpoint);
  const response = await fetch(`${connection.baseUrl}/stop`, {
    method: "POST",
    headers: jsonHeaders(connection),
    body: JSON.stringify({ runId }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function resolveApproval(
  endpoint: AgentEndpoint,
  approvalId: string,
  decision: "approved" | "denied",
): Promise<void> {
  const connection = normalizeEndpoint(endpoint);
  const response = await fetch(`${connection.baseUrl}/approve`, {
    method: "POST",
    headers: jsonHeaders(connection),
    body: JSON.stringify({ approvalId, decision }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function submitToolResult(
  endpoint: AgentEndpoint,
  input: ToolResultInput,
): Promise<void> {
  const connection = normalizeEndpoint(endpoint);
  const response = await fetch(`${connection.baseUrl}/tool_result`, {
    method: "POST",
    headers: jsonHeaders(connection),
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function streamAgent(
  endpoint: AgentEndpoint,
  input: {
    threadId?: string;
    messages: WireMessage[];
    tools?: ClientToolSpec[];
  },
  onEvent: (event: AGUIEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const connection = normalizeEndpoint(endpoint);
  const threadId = input.threadId ?? "";
  const agent = new HttpAgent({
    url: `${connection.baseUrl}/agent`,
    headers: connection.headers,
  });
  const runInput = {
    threadId,
    runId: threadId,
    messages: input.messages,
    tools: input.tools ?? [],
    context: [],
    state: {},
    forwardedProps: {},
  } as RunAgentInput;

  await new Promise<void>((resolve, reject) => {
    const subscription = agent.run(runInput).subscribe({
      next: (event: BaseEvent) => onEvent(event as AGUIEvent),
      error: reject,
      complete: resolve,
    });
    signal?.addEventListener(
      "abort",
      () => {
        agent.abortRun();
        subscription.unsubscribe();
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}
