import { HttpAgent, type BaseEvent, type RunAgentInput } from "@ag-ui/client";

export const DEFAULT_AGENT_URL =
  process.env.NEXT_PUBLIC_KNUTH_AGUI_URL ?? "http://127.0.0.1:8000";

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

export async function fetchThreads(baseUrl: string): Promise<ThreadSummary[]> {
  const response = await fetch(`${baseUrl}/threads`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const data = (await response.json()) as { threads?: ThreadSummary[] };
  return data.threads ?? [];
}

export async function fetchHistory(
  baseUrl: string,
  threadId: string,
): Promise<WireMessage[]> {
  const response = await fetch(`${baseUrl}/threads/${threadId}/history`, {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const data = (await response.json()) as { messages?: WireMessage[] };
  return data.messages ?? [];
}

export async function pauseRun(baseUrl: string, runId: string): Promise<void> {
  const response = await fetch(`${baseUrl}/pause`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ runId }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function resolveApproval(
  baseUrl: string,
  approvalId: string,
  decision: "approved" | "denied",
): Promise<void> {
  const response = await fetch(`${baseUrl}/approve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ approvalId, decision }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function submitToolResult(
  baseUrl: string,
  input: ToolResultInput,
): Promise<void> {
  const response = await fetch(`${baseUrl}/tool_result`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function streamAgent(
  baseUrl: string,
  input: {
    threadId?: string;
    messages: WireMessage[];
    tools?: ClientToolSpec[];
  },
  onEvent: (event: AGUIEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const threadId = input.threadId ?? "";
  const agent = new HttpAgent({ url: `${baseUrl}/agent` });
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
