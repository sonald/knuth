"use client";

import {
  AlertTriangle,
  ArrowUp,
  Brain,
  Bug,
  Check,
  ChevronRight,
  CircleStop,
  FilePen,
  FilePlus,
  FileText,
  FolderOpen,
  FolderSearch,
  KeyRound,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Save,
  Search,
  Settings2,
  ShieldAlert,
  Sparkles,
  Square,
  Terminal,
  User,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  FormEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  KnuthDesktopSettings,
  KnuthDesktopSettingsInput,
} from "../types/knuth-desktop";
import {
  AGUIEvent,
  AgentConnection,
  ClientToolSpec,
  DEFAULT_AGENT_URL,
  PendingApproval,
  ThreadSummary,
  WireMessage,
  fetchHistory,
  fetchPendingApprovals,
  fetchThreads,
  resolveApproval,
  stopRun,
  streamAgent,
  submitToolResult,
} from "../lib/agui";

type TimelineKind =
  | "run"
  | "user"
  | "assistant"
  | "thinking"
  | "tool"
  | "approval"
  | "error"
  | "context";

type TimelineStatus =
  | "queued"
  | "running"
  | "done"
  | "waiting"
  | "failed"
  | "approved"
  | "denied"
  | "info";

type TimelineItem = {
  id: string;
  kind: TimelineKind;
  title: string;
  label: string;
  body?: string;
  timestamp?: string;
  status: TimelineStatus;
  toolCallId?: string;
  approvalId?: string;
  args?: string;
  result?: string;
  raw?: unknown;
};

type ApprovalView = {
  approvalId: string;
  toolCallId: string;
  title: string;
  reason: string;
  risk: string;
  preview: string;
};

type ClientToolRequest = {
  runId: string;
  threadId: string;
  toolCallId: string;
  toolName: string;
  args: unknown;
};

type ThreadGroup = {
  label: string;
  threads: ThreadSummary[];
};

type SettingsDraft = {
  authMode: "api_key" | "chatgpt";
  modelBaseUrl: string;
  model: string;
  timeout: string;
  workspace: string;
  dbPath: string;
  apiKey: string;
  clearApiKey: boolean;
};

const EXAMPLE_PROMPTS = [
  "What files are in this project?",
  "Summarize the runtime architecture.",
  "Find every TODO in the codebase.",
];

const EMPTY_SETTINGS_DRAFT: SettingsDraft = {
  authMode: "api_key",
  modelBaseUrl: "",
  model: "",
  timeout: "60",
  workspace: "",
  dbPath: "",
  apiKey: "",
  clearApiKey: false,
};

const CLIENT_TOOLS: ClientToolSpec[] = [
  {
    name: "browser_context",
    description:
      "Return the current browser page context, locale, timezone, viewport, and client clock.",
    parameters: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
  },
];

function clientToolKey(request: Pick<ClientToolRequest, "runId" | "toolCallId">) {
  return `${request.runId}:${request.toolCallId}`;
}

async function executeClientTool(
  name: string,
  _args: unknown,
): Promise<unknown> {
  if (name !== "browser_context") {
    throw new Error(`Unknown client tool: ${name}`);
  }
  return {
    href: window.location.href,
    title: document.title,
    locale: navigator.language,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    now: new Date().toISOString(),
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
    },
  };
}

function renderContent(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (value == null) {
    return "";
  }
  if (Array.isArray(value)) {
    return value.map(renderContent).filter(Boolean).join("\n");
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function formatJsonish(value: unknown): string {
  const rendered = renderContent(value);
  const trimmed = rendered.trim();
  if (!trimmed || (!trimmed.startsWith("{") && !trimmed.startsWith("["))) {
    return rendered;
  }
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return rendered;
  }
}

function compactId(value: string | undefined): string {
  if (!value) {
    return "";
  }
  if (value.length <= 18) {
    return value;
  }
  return `${value.slice(0, 10)}…${value.slice(-5)}`;
}

function shortStatus(status: string): string {
  return status.replaceAll("_", " ");
}

function statusTone(status: string): TimelineStatus | "info" {
  if (["succeeded", "done", "approved"].includes(status)) {
    return "done";
  }
  if (["running", "queued"].includes(status)) {
    return "running";
  }
  if (["waiting", "waiting_approval", "waiting_tool_result", "paused"].includes(status)) {
    return "waiting";
  }
  if (["failed", "denied"].includes(status)) {
    return "failed";
  }
  return "info";
}

function nowTime(): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date());
}

function formatThreadTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatThreadDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Earlier";
  }
  const today = new Date();
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const startOfDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round(
    (startOfToday.getTime() - startOfDate.getTime()) / 86_400_000,
  );
  if (days === 0) {
    return "Today";
  }
  if (days === 1) {
    return "Yesterday";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  }).format(date);
}

// -- markdown rendering (react-markdown + GFM, XSS-safe by default) ------

function MarkdownView({ text }: { text: string }) {
  return (
    <div className="prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer noopener" />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function historyToTimeline(messages: WireMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const toolIndexById = new Map<string, number>();

  messages.forEach((message, index) => {
    const id = message.id ?? `history_${index}`;
    const content = renderContent(message.content).trim();

    if (message.role === "user") {
      items.push({ id, kind: "user", title: "You", label: "Input", body: content, status: "done" });
      return;
    }

    if (message.role === "assistant") {
      if (content) {
        items.push({
          id,
          kind: "assistant",
          title: "Knuth",
          label: "Output",
          body: content,
          status: "done",
        });
      }
      for (const call of message.toolCalls ?? []) {
        const toolId = `tool_${call.id}`;
        toolIndexById.set(call.id, items.length);
        items.push({
          id: toolId,
          kind: "tool",
          title: call.function?.name ?? "tool",
          label: "Done",
          status: "done",
          toolCallId: call.id,
          args: formatJsonish(call.function?.arguments ?? ""),
        });
      }
      return;
    }

    if (message.role === "tool") {
      // Attach the result to the planned call so one card shows name+args+result.
      const existing = message.toolCallId
        ? toolIndexById.get(message.toolCallId)
        : undefined;
      if (existing !== undefined) {
        items[existing] = { ...items[existing], result: content, status: "done" };
        return;
      }
      items.push({
        id: `tool_${message.toolCallId ?? id}_result`,
        kind: "tool",
        title: "tool result",
        label: "Result",
        result: content,
        status: "done",
        toolCallId: message.toolCallId,
      });
      return;
    }

    if (content) {
      items.push({
        id,
        kind: "context",
        title: `${message.role} note`,
        label: "Context",
        body: content,
        status: "info",
      });
    }
  });

  return items;
}

function approvalToView(approval: PendingApproval): ApprovalView {
  return {
    approvalId: approval.approvalId,
    toolCallId: approval.toolCallId,
    title: approval.title || "approval requested",
    reason: approval.reason || "",
    risk: approval.risk || "",
    preview: formatJsonish(approval.preview ?? ""),
  };
}

function appendApprovalItems(
  items: TimelineItem[],
  approvals: ApprovalView[],
): TimelineItem[] {
  return approvals.reduce(
    (current, approval) =>
      upsertItem(current, {
        id: `approval_${approval.approvalId}`,
        kind: "approval",
        title: approval.title,
        label: "Approval",
        body: approval.reason || approval.title,
        args: approval.preview,
        status: "waiting",
        toolCallId: approval.toolCallId,
        approvalId: approval.approvalId,
      }),
    items,
  );
}

function groupThreads(threads: ThreadSummary[]): ThreadGroup[] {
  const groups = new Map<string, ThreadSummary[]>();
  for (const thread of threads) {
    const label = formatThreadDate(thread.updatedAt || thread.createdAt);
    groups.set(label, [...(groups.get(label) ?? []), thread]);
  }
  return Array.from(groups, ([label, groupedThreads]) => ({
    label,
    threads: groupedThreads,
  }));
}

function upsertItem(items: TimelineItem[], next: TimelineItem): TimelineItem[] {
  const index = items.findIndex((item) => item.id === next.id);
  if (index === -1) {
    return [...items, next];
  }
  const copy = [...items];
  copy[index] = { ...copy[index], ...next };
  return copy;
}

function appendItemText(
  items: TimelineItem[],
  id: string,
  delta: string,
  fallback: TimelineItem,
): TimelineItem[] {
  const index = items.findIndex((item) => item.id === id);
  if (index === -1) {
    return [...items, { ...fallback, body: delta }];
  }
  const copy = [...items];
  copy[index] = { ...copy[index], body: `${copy[index].body ?? ""}${delta}` };
  return copy;
}

function appendItemArgs(
  items: TimelineItem[],
  id: string,
  delta: string,
  fallback: TimelineItem,
): TimelineItem[] {
  const index = items.findIndex((item) => item.id === id);
  if (index === -1) {
    return [...items, { ...fallback, args: delta }];
  }
  const copy = [...items];
  copy[index] = { ...copy[index], args: `${copy[index].args ?? ""}${delta}` };
  return copy;
}

function threadTitle(thread: ThreadSummary | undefined): string {
  if (!thread) {
    return "New conversation";
  }
  return thread.query?.trim() || compactId(thread.threadId);
}

function settingsDraftFrom(settings: KnuthDesktopSettings): SettingsDraft {
  return {
    authMode: settings.authMode,
    modelBaseUrl: settings.modelBaseUrl,
    model: settings.model,
    timeout: String(settings.timeout || 60),
    workspace: settings.workspace,
    dbPath: settings.dbPath,
    apiKey: "",
    clearApiKey: false,
  };
}

function desktopConnectionFrom(backend: AgentConnection): AgentConnection {
  return {
    baseUrl: backend.baseUrl,
    headers: backend.headers ?? {},
    status: backend.status,
    mode: backend.mode,
    workspace: backend.workspace,
    settings: backend.settings,
    chatgptLogin: backend.chatgptLogin,
    error: backend.error,
  };
}

export function KnuthIMApp() {
  const [baseUrl, setBaseUrl] = useState(DEFAULT_AGENT_URL);
  const [desktopConnection, setDesktopConnection] =
    useState<AgentConnection | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string>();
  const [timelineItems, setTimelineItems] = useState<TimelineItem[]>([]);
  const [approvals, setApprovals] = useState<ApprovalView[]>([]);
  const [clientToolQueue, setClientToolQueue] = useState<ClientToolRequest[]>([]);
  const [draft, setDraft] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string>();
  const [connected, setConnected] = useState(false);
  const [desktopDetected, setDesktopDetected] = useState(false);
  const [desktopLoading, setDesktopLoading] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [autoApprove, setAutoApprove] = useState(false);
  const [desktopSettings, setDesktopSettings] =
    useState<KnuthDesktopSettings | null>(null);
  const [settingsDraft, setSettingsDraft] =
    useState<SettingsDraft>(EMPTY_SETTINGS_DRAFT);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState<string>();
  const [settingsMessage, setSettingsMessage] = useState<string>();

  const abortRef = useRef<AbortController | null>(null);
  const thinkingIdRef = useRef<string | null>(null);
  const submittedClientToolsRef = useRef<Set<string>>(new Set());
  const processingClientToolsRef = useRef(false);
  const processingApprovalRef = useRef(false);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);

  const activeThread = useMemo(
    () => threads.find((thread) => thread.threadId === activeThreadId),
    [activeThreadId, threads],
  );

  const groupedThreads = useMemo(() => groupThreads(threads), [threads]);
  const backendNeedsSettings = desktopConnection?.status === "needs_settings";
  const desktopBackendFailed = desktopConnection?.status === "failed";
  const desktopBackendStarting =
    desktopDetected &&
    (desktopLoading ||
      desktopConnection === null ||
      desktopConnection.status === "starting");
  const backendUnavailable =
    Boolean(backendNeedsSettings) || desktopBackendFailed || desktopBackendStarting;
  const desktopMode = Boolean(desktopConnection || desktopSettings);
  const agentConnection = useMemo<AgentConnection>(() => {
    if (desktopConnection && desktopConnection.baseUrl === baseUrl) {
      return desktopConnection;
    }
    return { baseUrl, headers: {} };
  }, [baseUrl, desktopConnection]);

  const canResume =
    activeThread?.status === "waiting_approval" ||
    activeThread?.status === "waiting_tool_result" ||
    activeThread?.status === "paused";

  // In the desktop app this opens a dedicated event-viewer window (main
  // process). In the browser it falls back to the /debug route.
  const openEventViewer = useCallback(() => {
    if (window.knuthDesktop?.openEventViewer) {
      void window.knuthDesktop.openEventViewer();
      return;
    }
    window.open("/debug", "_blank", "noopener");
  }, []);

  const connectionLabel = useMemo(() => {
    if (backendNeedsSettings) {
      return "Settings needed";
    }
    if (desktopBackendFailed) {
      return "Backend failed";
    }
    if (desktopBackendStarting) {
      return "Starting";
    }
    return connected ? "Connected" : "Disconnected";
  }, [backendNeedsSettings, connected, desktopBackendFailed, desktopBackendStarting]);

  const statusLabel = useMemo(() => {
    if (running) {
      return "Working…";
    }
    if (desktopBackendStarting) {
      return "Starting backend…";
    }
    if (activeThread) {
      return `${shortStatus(activeThread.status)} · ${activeThread.steps} steps`;
    }
    return "Ready";
  }, [activeThread, running, desktopBackendStarting]);

  const refresh = useCallback(async () => {
    if (desktopBackendStarting) {
      setConnected(false);
      return;
    }
    if (backendNeedsSettings) {
      setConnected(false);
      setError("Model settings required");
      return;
    }
    if (desktopBackendFailed) {
      setConnected(false);
      setError(desktopConnection?.error ?? "Backend failed");
      return;
    }
    try {
      const nextThreads = await fetchThreads(agentConnection);
      setThreads(nextThreads);
      setConnected(true);
      setError(undefined);
    } catch {
      setConnected(false);
      setError("AG-UI backend unavailable");
    }
  }, [
    agentConnection,
    backendNeedsSettings,
    desktopConnection,
    desktopBackendFailed,
    desktopConnection?.error,
    desktopBackendStarting,
  ]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    let cancelled = false;
    async function loadDesktopBackend() {
      if (!window.knuthDesktop?.backend) {
        return;
      }
      setDesktopDetected(true);
      setDesktopLoading(true);
      setConnected(false);
      try {
        const settings = await window.knuthDesktop.getSettings?.();
        if (cancelled) {
          return;
        }
        if (settings) {
          setDesktopSettings(settings);
          setSettingsDraft(settingsDraftFrom(settings));
          if (!settings.ready) {
            setShowSettings(true);
          }
        }
        const backend = await window.knuthDesktop.backend();
        if (cancelled) {
          return;
        }
        const next = desktopConnectionFrom(backend);
        setDesktopConnection(next);
        setBaseUrl(next.baseUrl);
        if (backend.status === "needs_settings") {
          setConnected(false);
          setError("Model settings required");
          setShowSettings(true);
        } else if (backend.status === "failed" && backend.error) {
          setConnected(false);
          setError(backend.error);
        }
      } catch (err) {
        if (!cancelled) {
          setError(String(err));
          setConnected(false);
        }
      } finally {
        if (!cancelled) {
          setDesktopLoading(false);
        }
      }
    }
    void loadDesktopBackend();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const node = transcriptRef.current;
    if (node && atBottomRef.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, [timelineItems]);

  const onTranscriptScroll = useCallback(() => {
    const node = transcriptRef.current;
    if (!node) {
      return;
    }
    atBottomRef.current =
      node.scrollHeight - node.scrollTop - node.clientHeight < 80;
  }, []);

  const chooseWorkspace = useCallback(async () => {
    try {
      const selected = await window.knuthDesktop?.chooseWorkspace?.();
      if (selected) {
        setSettingsDraft((current) => ({ ...current, workspace: selected }));
        setSettingsMessage(undefined);
      }
    } catch (err) {
      setSettingsError(String(err));
    }
  }, []);

  const saveDesktopSettings = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!window.knuthDesktop?.saveSettings) {
        return;
      }
      setSettingsSaving(true);
      setSettingsError(undefined);
      setSettingsMessage(undefined);
      const payload: KnuthDesktopSettingsInput = {
        authMode: settingsDraft.authMode,
        modelBaseUrl: settingsDraft.modelBaseUrl,
        model: settingsDraft.model,
        timeout: settingsDraft.timeout,
        workspace: settingsDraft.workspace,
        dbPath: settingsDraft.dbPath,
      };
      if (settingsDraft.apiKey.trim()) {
        payload.apiKey = settingsDraft.apiKey;
      }
      if (settingsDraft.clearApiKey) {
        payload.clearApiKey = true;
      }

      try {
        const result = await window.knuthDesktop.saveSettings(payload);
        setDesktopSettings(result.settings);
        setSettingsDraft(settingsDraftFrom(result.settings));
        setSettingsMessage("Saved");
        const next = desktopConnectionFrom(result.backend);
        setDesktopConnection(next);
        setBaseUrl(next.baseUrl);
        if (next.status === "ready" || next.status === "external") {
          const nextThreads = await fetchThreads(next);
          setThreads(nextThreads);
          setConnected(true);
          setError(undefined);
        } else if (next.status === "needs_settings") {
          setConnected(false);
          setError("Model settings required");
          setShowSettings(true);
        } else if (next.status === "failed") {
          setConnected(false);
          setError(next.error ?? "Backend failed");
        }
      } catch (err) {
        setSettingsError(err instanceof Error ? err.message : String(err));
      } finally {
        setSettingsSaving(false);
      }
    },
    [settingsDraft],
  );

  const clearChatgptAuth = useCallback(async () => {
    if (!window.knuthDesktop?.clearChatgptAuth) {
      return;
    }
    setSettingsSaving(true);
    setSettingsError(undefined);
    setSettingsMessage(undefined);
    try {
      const result = await window.knuthDesktop.clearChatgptAuth();
      setDesktopSettings(result.settings);
      setSettingsDraft(settingsDraftFrom(result.settings));
      const next = desktopConnectionFrom(result.backend);
      setDesktopConnection(next);
      setBaseUrl(next.baseUrl);
      setSettingsMessage("ChatGPT login cleared");
    } catch (err) {
      setSettingsError(String(err));
    } finally {
      setSettingsSaving(false);
    }
  }, []);

  const verifyChatgptLogin = useCallback(() => {
    setSettingsError(undefined);
    setSettingsMessage("Waiting for ChatGPT login…");
    if (window.knuthDesktop?.restartBackend) {
      void window.knuthDesktop
        .restartBackend()
        .then((backend) => {
          const next = desktopConnectionFrom(backend);
          setDesktopConnection(next);
          setBaseUrl(next.baseUrl);
          if (backend.chatgptLogin) {
            setSettingsMessage(undefined);
          }
          void fetch(`${next.baseUrl}/agent`, {
            method: "POST",
            headers: {
              "content-type": "application/json",
              ...(next.headers ?? {}),
            },
            body: JSON.stringify({
              messages: [{ role: "user", content: "Reply with ok." }],
            }),
          }).catch(() => {
            setSettingsError("ChatGPT login check failed");
          });
        })
        .catch((err) => {
          setSettingsError(err instanceof Error ? err.message : String(err));
        });
      return;
    }
    void fetch(`${agentConnection.baseUrl}/agent`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(agentConnection.headers ?? {}),
      },
      body: JSON.stringify({
        messages: [{ role: "user", content: "Reply with ok." }],
      }),
    }).catch(() => {
      setSettingsError("ChatGPT login check failed");
    });
  }, [agentConnection]);

  useEffect(() => {
    if (
      !showSettings ||
      desktopSettings?.authMode !== "chatgpt" ||
      !window.knuthDesktop?.backend
    ) {
      return;
    }
    const interval = window.setInterval(() => {
      void window.knuthDesktop?.backend?.().then((backend) => {
        const next = desktopConnectionFrom(backend);
        setDesktopConnection(next);
        setBaseUrl(next.baseUrl);
        if (backend.chatgptLogin) {
          setSettingsMessage(undefined);
        }
      });
    }, 1000);
    return () => window.clearInterval(interval);
  }, [desktopSettings?.authMode, showSettings]);

  const restartDesktopBackend = useCallback(async () => {
    if (!window.knuthDesktop?.restartBackend) {
      return;
    }
    setSettingsError(undefined);
    setSettingsMessage(undefined);
    try {
      const backend = await window.knuthDesktop.restartBackend();
      const next = desktopConnectionFrom(backend);
      setDesktopConnection(next);
      setBaseUrl(next.baseUrl);
      setSettingsMessage("Restarted");
      if (next.status === "ready" || next.status === "external") {
        const nextThreads = await fetchThreads(next);
        setThreads(nextThreads);
        setConnected(true);
        setError(undefined);
      } else if (next.status === "needs_settings") {
        setConnected(false);
        setError("Model settings required");
        setShowSettings(true);
      } else if (next.status === "failed") {
        setConnected(false);
        setError(next.error ?? "Backend failed");
      }
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const applyEvent = useCallback(
    (event: AGUIEvent) => {
      switch (event.type) {
        case "RUN_STARTED": {
          const threadId = String(event.threadId ?? "");
          const runId = String(event.runId ?? threadId);
          if (threadId) {
            setActiveThreadId(threadId);
          }
          setRunning(true);
          setError(undefined);
          setTimelineItems((current) =>
            upsertItem(current, {
              id: `run_${runId}`,
              kind: "run",
              title: "Run started",
              label: "Run",
              timestamp: nowTime(),
              status: "running",
            }),
          );
          break;
        }
        case "MESSAGES_SNAPSHOT": {
          const messages = Array.isArray(event.messages)
            ? (event.messages as WireMessage[])
            : [];
          setTimelineItems(historyToTimeline(messages));
          break;
        }
        case "THINKING_START":
        case "THINKING_TEXT_MESSAGE_START": {
          if (!thinkingIdRef.current) {
            thinkingIdRef.current = `thinking_${Date.now()}`;
          }
          const itemId = thinkingIdRef.current;
          setTimelineItems((current) =>
            upsertItem(current, {
              id: itemId,
              kind: "thinking",
              title: "Reasoning",
              label: "Thinking",
              timestamp: nowTime(),
              status: "running",
            }),
          );
          break;
        }
        case "THINKING_TEXT_MESSAGE_CONTENT": {
          if (!thinkingIdRef.current) {
            thinkingIdRef.current = `thinking_${Date.now()}`;
          }
          const itemId = thinkingIdRef.current;
          const delta = String(event.delta ?? "");
          setTimelineItems((current) =>
            appendItemText(current, itemId, delta, {
              id: itemId,
              kind: "thinking",
              title: "Reasoning",
              label: "Thinking",
              timestamp: nowTime(),
              status: "running",
            }),
          );
          break;
        }
        case "THINKING_TEXT_MESSAGE_END":
        case "THINKING_END": {
          const itemId = thinkingIdRef.current;
          if (itemId) {
            setTimelineItems((current) =>
              current.map((item) =>
                item.id === itemId ? { ...item, status: "done" as const } : item,
              ),
            );
          }
          if (event.type === "THINKING_END") {
            thinkingIdRef.current = null;
          }
          break;
        }
        case "TEXT_MESSAGE_START": {
          const id = String(event.messageId);
          setTimelineItems((current) =>
            upsertItem(current, {
              id,
              kind: "assistant",
              title: "Knuth",
              label: "Output",
              timestamp: nowTime(),
              status: "running",
            }),
          );
          break;
        }
        case "TEXT_MESSAGE_CONTENT": {
          const id = String(event.messageId);
          const delta = String(event.delta ?? "");
          setTimelineItems((current) =>
            appendItemText(current, id, delta, {
              id,
              kind: "assistant",
              title: "Knuth",
              label: "Output",
              timestamp: nowTime(),
              status: "running",
            }),
          );
          break;
        }
        case "TEXT_MESSAGE_END": {
          const id = String(event.messageId);
          setTimelineItems((current) =>
            current.map((item) =>
              item.id === id ? { ...item, status: "done" as const } : item,
            ),
          );
          break;
        }
        case "TOOL_CALL_START": {
          const toolCallId = String(event.toolCallId);
          setTimelineItems((current) =>
            upsertItem(current, {
              id: `tool_${toolCallId}`,
              kind: "tool",
              title: String(event.toolCallName ?? "tool"),
              label: "Started",
              body: String(event.toolCallName ?? "tool"),
              timestamp: nowTime(),
              status: "running",
              toolCallId,
            }),
          );
          break;
        }
        case "TOOL_CALL_ARGS": {
          const toolCallId = String(event.toolCallId);
          const delta = String(event.delta ?? "");
          setTimelineItems((current) =>
            appendItemArgs(current, `tool_${toolCallId}`, delta, {
              id: `tool_${toolCallId}`,
              kind: "tool",
              title: "tool",
              label: "Started",
              timestamp: nowTime(),
              status: "running",
              toolCallId,
            }),
          );
          break;
        }
        case "TOOL_CALL_RESULT": {
          const toolCallId = String(event.toolCallId);
          const result = renderContent(event.content);
          setTimelineItems((current) =>
            current.map((item) =>
              item.id === `tool_${toolCallId}`
                ? {
                    ...item,
                    label: "Done",
                    status: "done" as const,
                    result,
                  }
                : item,
            ),
          );
          break;
        }
        case "CUSTOM": {
          if (event.name === "knuth.tool_result_required") {
            const value = event.value as Partial<ClientToolRequest> | undefined;
            if (!value?.runId || !value?.toolCallId || !value?.toolName) {
              break;
            }
            const request: ClientToolRequest = {
              runId: value.runId,
              threadId: value.threadId || value.runId,
              toolCallId: value.toolCallId,
              toolName: value.toolName,
              args: value.args ?? {},
            };
            setTimelineItems((current) =>
              upsertItem(current, {
                id: `tool_${request.toolCallId}`,
                kind: "tool",
                title: request.toolName,
                label: "Client",
                body: request.toolName,
                args: formatJsonish(request.args),
                timestamp: nowTime(),
                status: "waiting",
                toolCallId: request.toolCallId,
              }),
            );
            setClientToolQueue((current) => {
              const key = clientToolKey(request);
              if (
                submittedClientToolsRef.current.has(key) ||
                current.some((item) => clientToolKey(item) === key)
              ) {
                return current;
              }
              return [...current, request];
            });
            break;
          }

          if (event.name !== "knuth.approval_requested") {
            break;
          }
          const value = event.value as Partial<ApprovalView> | undefined;
          if (!value?.approvalId) {
            break;
          }
          const approval: ApprovalView = {
            approvalId: value.approvalId,
            toolCallId: value.toolCallId ?? "",
            title: value.title ?? "approval requested",
            reason: value.reason ?? "",
            risk: value.risk ?? "",
            preview: formatJsonish(value.preview ?? ""),
          };
          setApprovals((current) => [
            ...current.filter((item) => item.approvalId !== approval.approvalId),
            approval,
          ]);
          setTimelineItems((current) =>
            upsertItem(current, {
              id: `approval_${approval.approvalId}`,
              kind: "approval",
              title: approval.title,
              label: "Approval",
              body: approval.reason || approval.title,
              args: approval.preview,
              timestamp: nowTime(),
              status: "waiting",
              toolCallId: approval.toolCallId,
              approvalId: approval.approvalId,
            }),
          );
          break;
        }
        case "RUN_ERROR": {
          const message = String(event.message ?? "run failed");
          setRunning(false);
          setError(message);
          setTimelineItems((current) => [
            ...current,
            {
              id: `error_${Date.now()}`,
              kind: "error",
              title: "Run error",
              label: "Error",
              body: message,
              timestamp: nowTime(),
              status: "failed",
              raw: event,
            },
          ]);
          break;
        }
        case "RUN_FINISHED": {
          const threadId = String(event.threadId ?? activeThreadId ?? "");
          const runId = String(event.runId ?? threadId);
          setRunning(false);
          setTimelineItems((current) =>
            current.map((item) =>
              item.id === `run_${runId}`
                ? { ...item, title: "Completed", status: "done" as const }
                : item,
            ),
          );
          void refresh();
          break;
        }
      }
    },
    [activeThreadId, refresh],
  );

  const runStream = useCallback(
    async (payload: { threadId?: string; messages: WireMessage[] }) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setRunning(true);
      setError(undefined);
      try {
        await streamAgent(
          agentConnection,
          { ...payload, tools: CLIENT_TOOLS },
          applyEvent,
          controller.signal,
        );
      } catch (err) {
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          setError(String(err));
        }
      } finally {
        setRunning(false);
        abortRef.current = null;
        void refresh();
      }
    },
    [agentConnection, applyEvent, refresh],
  );

  useEffect(() => {
    if (running || clientToolQueue.length === 0 || processingClientToolsRef.current) {
      return;
    }
    const requests = [...clientToolQueue];
    const processedKeys = new Set(requests.map(clientToolKey));
    processingClientToolsRef.current = true;

    async function executeAndResume() {
      try {
        let resumeThreadId: string | undefined;
        let submittedAny = false;
        for (const request of requests) {
          const key = clientToolKey(request);
          if (submittedClientToolsRef.current.has(key)) {
            continue;
          }
          submittedClientToolsRef.current.add(key);
          resumeThreadId = request.threadId || request.runId;
          const id = `tool_${request.toolCallId}`;
          setTimelineItems((current) =>
            upsertItem(current, {
              id,
              kind: "tool",
              title: request.toolName,
              label: "Executing",
              body: request.toolName,
              args: formatJsonish(request.args),
              timestamp: nowTime(),
              status: "running",
              toolCallId: request.toolCallId,
            }),
          );
          try {
            const result = await executeClientTool(request.toolName, request.args);
            await submitToolResult(agentConnection, {
              runId: request.runId,
              toolCallId: request.toolCallId,
              outcome: "succeeded",
              result,
            });
            submittedAny = true;
            setTimelineItems((current) =>
              upsertItem(current, {
                id,
                kind: "tool",
                title: request.toolName,
                label: "Done",
                body: request.toolName,
                args: formatJsonish(request.args),
                result: renderContent(result),
                timestamp: nowTime(),
                status: "done",
                toolCallId: request.toolCallId,
              }),
            );
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            try {
              await submitToolResult(agentConnection, {
                runId: request.runId,
                toolCallId: request.toolCallId,
                outcome: "failed",
                error: message,
              });
              submittedAny = true;
            } catch (submitErr) {
              submittedClientToolsRef.current.delete(key);
              setError(String(submitErr));
            }
            setTimelineItems((current) =>
              upsertItem(current, {
                id,
                kind: "tool",
                title: request.toolName,
                label: "Failed",
                body: request.toolName,
                args: formatJsonish(request.args),
                result: message,
                timestamp: nowTime(),
                status: "failed",
                toolCallId: request.toolCallId,
              }),
            );
          }
        }

        if (submittedAny && resumeThreadId) {
          await runStream({ threadId: resumeThreadId, messages: [] });
        }
      } finally {
        processingClientToolsRef.current = false;
        setClientToolQueue((current) =>
          current.filter((request) => !processedKeys.has(clientToolKey(request))),
        );
      }
    }

    void executeAndResume();
  }, [agentConnection, clientToolQueue, runStream, running]);

  const sendMessage = useCallback(
    async (content: string) => {
      const trimmed = content.trim();
      if (backendNeedsSettings) {
        setShowSettings(true);
        setError("Model settings required");
        return;
      }
      if (desktopBackendStarting) {
        setError("Backend is still starting");
        return;
      }
      if (desktopBackendFailed) {
        setError(desktopConnection?.error ?? "Backend failed");
        return;
      }
      if (!trimmed || running) {
        return;
      }
      setDraft("");
      setApprovals([]);
      setClientToolQueue([]);
      thinkingIdRef.current = null;
      atBottomRef.current = true;
      const userItem: TimelineItem = {
        id: `local_${Date.now()}`,
        kind: "user",
        title: "You",
        label: "Input",
        body: trimmed,
        timestamp: nowTime(),
        status: "done",
      };
      setTimelineItems((current) => [...current, userItem]);
      await runStream({
        threadId: activeThreadId,
        messages: [{ role: "user", content: trimmed }],
      });
    },
    [
      activeThreadId,
      backendNeedsSettings,
      desktopBackendFailed,
      desktopBackendStarting,
      desktopConnection?.error,
      running,
      runStream,
    ],
  );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void sendMessage(draft);
  };

  const selectThread = async (threadId: string) => {
    abortRef.current?.abort();
    setRunning(false);
    setActiveThreadId(threadId);
    setApprovals([]);
    setClientToolQueue([]);
    setAutoApprove(false);
    thinkingIdRef.current = null;
    setError(undefined);
    atBottomRef.current = true;
    try {
      const [history, pendingApprovals] = await Promise.all([
        fetchHistory(agentConnection, threadId),
        fetchPendingApprovals(agentConnection, threadId),
      ]);
      const approvalViews = pendingApprovals.map(approvalToView);
      setApprovals(approvalViews);
      setTimelineItems(appendApprovalItems(historyToTimeline(history), approvalViews));
    } catch (err) {
      setError(String(err));
    }
  };

  const newThread = () => {
    abortRef.current?.abort();
    setRunning(false);
    setActiveThreadId(undefined);
    setTimelineItems([]);
    setApprovals([]);
    setClientToolQueue([]);
    setAutoApprove(false);
    thinkingIdRef.current = null;
    setError(undefined);
  };

  // UI stop: interrupt active work server-side, then drop the local SSE
  // subscription. Aborting the stream is only an unsubscribe now (the backend
  // live manager keeps the run), so the interrupt must go through /stop.
  const stop = async () => {
    if (!activeThreadId) {
      abortRef.current?.abort();
      setRunning(false);
      return;
    }
    try {
      await stopRun(agentConnection, activeThreadId);
    } catch (err) {
      setError(String(err));
    }
    abortRef.current?.abort();
    setRunning(false);
    void refresh();
  };

  const resume = () => {
    if (!activeThreadId || running) {
      return;
    }
    void runStream({ threadId: activeThreadId, messages: [] });
  };

  const decide = useCallback(
    async (approval: ApprovalView, decision: "approved" | "denied") => {
      const decisionStatus: TimelineStatus =
        decision === "approved" ? "approved" : "denied";
      try {
        await resolveApproval(agentConnection, approval.approvalId, decision);
      } catch (err) {
        setError(String(err));
        return;
      }
      setApprovals((current) =>
        current.filter((item) => item.approvalId !== approval.approvalId),
      );
      setTimelineItems((current) =>
        current.map((item) =>
          item.approvalId === approval.approvalId
            ? {
                ...item,
                label: decision === "approved" ? "Approved" : "Denied",
                status: decisionStatus,
              }
            : item,
        ),
      );
      await runStream({ threadId: activeThreadId, messages: [] });
    },
    [activeThreadId, agentConnection, runStream],
  );

  // "Approve all" → auto-resolve pending approvals for the rest of the session.
  useEffect(() => {
    if (!autoApprove || running || processingApprovalRef.current) {
      return;
    }
    const next = approvals[0];
    if (!next) {
      return;
    }
    processingApprovalRef.current = true;
    void (async () => {
      try {
        await decide(next, "approved");
      } finally {
        processingApprovalRef.current = false;
      }
    })();
  }, [autoApprove, running, approvals, decide]);

  return (
    <div className="app">
      <aside className="sidebar" aria-label="Conversations">
        <div className="brand">
          <div className="brandMark">K</div>
          <div>
            <div className="brandName">Knuth</div>
            <div className="brandSub">literate agent</div>
          </div>
        </div>

        <button className="newChat" type="button" onClick={newThread}>
          <Plus size={17} />
          <span>New conversation</span>
        </button>

        <div className="convList">
          {groupedThreads.length ? (
            groupedThreads.map((group) => (
              <section key={group.label} className="convGroup">
                <div className="convGroupLabel">{group.label}</div>
                {group.threads.map((thread) => (
                  <button
                    key={thread.threadId}
                    type="button"
                    className={
                      thread.threadId === activeThreadId
                        ? "convItem active"
                        : "convItem"
                    }
                    onClick={() => void selectThread(thread.threadId)}
                  >
                    <span className={`convDot ${statusTone(thread.status)}`} />
                    <span className="convBody">
                      <span className="convTitle">{threadTitle(thread)}</span>
                      <span className="convMeta">
                        <span className="convStatus">{shortStatus(thread.status)}</span>
                        <span>·</span>
                        <span>{formatThreadTime(thread.updatedAt || thread.createdAt)}</span>
                      </span>
                    </span>
                  </button>
                ))}
              </section>
            ))
          ) : (
            <div className="convEmpty">No conversations yet</div>
          )}
        </div>

        <div className="sidebarFoot">
          <button
            type="button"
            className={showSettings ? "endpointToggle open" : "endpointToggle"}
            onClick={() => setShowSettings((value) => !value)}
          >
            <span
              className={
                connected && !backendNeedsSettings && !desktopBackendFailed
                  ? "connDot"
                  : "connDot down"
              }
            />
            <span>{connectionLabel}</span>
            <Settings2 size={14} className="chev" />
          </button>
          {showSettings ? (
            <div className="settingsPanel">
              {desktopMode ? (
                <form className="settingsForm" onSubmit={saveDesktopSettings}>
                  <label className="settingsField">
                    <span>Auth</span>
                    <select
                      value={settingsDraft.authMode}
                      onChange={(event) => {
                        const authMode = event.target
                          .value as SettingsDraft["authMode"];
                        setSettingsMessage(undefined);
                        setSettingsDraft((current) => ({
                          ...current,
                          authMode,
                          modelBaseUrl:
                            authMode === "chatgpt" ? "" : current.modelBaseUrl,
                          apiKey: authMode === "chatgpt" ? "" : current.apiKey,
                          clearApiKey:
                            authMode === "chatgpt" ? false : current.clearApiKey,
                        }));
                      }}
                    >
                      <option value="api_key">API key</option>
                      <option value="chatgpt">ChatGPT subscription</option>
                    </select>
                  </label>
                  {settingsDraft.authMode === "api_key" ? (
                    <label className="settingsField">
                      <span>Model endpoint</span>
                      <input
                        value={settingsDraft.modelBaseUrl}
                        onChange={(event) => {
                          setSettingsMessage(undefined);
                          setSettingsDraft((current) => ({
                            ...current,
                            modelBaseUrl: event.target.value,
                          }));
                        }}
                        placeholder="https://api.example.com/v1"
                        spellCheck={false}
                      />
                    </label>
                  ) : null}
                  <label className="settingsField">
                    <span>Model</span>
                    <input
                      value={settingsDraft.model}
                      onChange={(event) => {
                        setSettingsMessage(undefined);
                        setSettingsDraft((current) => ({
                          ...current,
                          model: event.target.value,
                        }));
                      }}
                      placeholder="provider/model"
                      spellCheck={false}
                    />
                  </label>
                  {settingsDraft.authMode === "api_key" ? (
                    <label className="settingsField">
                      <span>
                        API key
                        {desktopSettings?.hasApiKey ? (
                          <span className="savedKey">saved</span>
                        ) : null}
                      </span>
                      <span className="secretInput">
                        <KeyRound size={14} />
                        <input
                          type="password"
                          value={settingsDraft.apiKey}
                          onChange={(event) => {
                            setSettingsMessage(undefined);
                            setSettingsDraft((current) => ({
                              ...current,
                              apiKey: event.target.value,
                              clearApiKey: false,
                            }));
                          }}
                          placeholder={
                            desktopSettings?.hasApiKey ? "Saved API key" : "API key"
                          }
                          spellCheck={false}
                        />
                      </span>
                    </label>
                  ) : (
                    <div className="settingsNotice">
                      {desktopSettings?.needsLogin
                        ? "ChatGPT login required"
                        : "ChatGPT login ready"}
                      <button
                        type="button"
                        className="inlineSettingsButton"
                        onClick={verifyChatgptLogin}
                      >
                        Login/Verify
                      </button>
                    </div>
                  )}
                  {settingsDraft.authMode === "api_key" &&
                  desktopSettings?.hasApiKey ? (
                    <label className="settingsCheck">
                      <input
                        type="checkbox"
                        checked={settingsDraft.clearApiKey}
                        onChange={(event) => {
                          setSettingsMessage(undefined);
                          setSettingsDraft((current) => ({
                            ...current,
                            clearApiKey: event.target.checked,
                            apiKey: event.target.checked ? "" : current.apiKey,
                          }));
                        }}
                      />
                      <span>Forget saved key</span>
                    </label>
                  ) : null}
                  <label className="settingsField">
                    <span>Workspace</span>
                    <span className="pathInput">
                      <input
                        value={settingsDraft.workspace}
                        onChange={(event) => {
                          setSettingsMessage(undefined);
                          setSettingsDraft((current) => ({
                            ...current,
                            workspace: event.target.value,
                          }));
                        }}
                        spellCheck={false}
                      />
                      <button
                        type="button"
                        className="pathButton"
                        title="Choose workspace"
                        onClick={() => void chooseWorkspace()}
                      >
                        <FolderOpen size={15} />
                      </button>
                    </span>
                  </label>
                  <label className="settingsField">
                    <span>Database</span>
                    <input
                      value={settingsDraft.dbPath}
                      onChange={(event) => {
                        setSettingsMessage(undefined);
                        setSettingsDraft((current) => ({
                          ...current,
                          dbPath: event.target.value,
                        }));
                      }}
                      spellCheck={false}
                    />
                  </label>
                  <label className="settingsField short">
                    <span>Timeout</span>
                    <input
                      type="number"
                      min="1"
                      max="3600"
                      step="1"
                      value={settingsDraft.timeout}
                      onChange={(event) => {
                        setSettingsMessage(undefined);
                        setSettingsDraft((current) => ({
                          ...current,
                          timeout: event.target.value,
                        }));
                      }}
                    />
                  </label>
                  <div className="settingsMeta">
                    <span>AG-UI</span>
                    <code>{baseUrl}</code>
                  </div>
                  {settingsError ? (
                    <div className="settingsNotice danger">{settingsError}</div>
                  ) : settingsMessage ? (
                    <div className="settingsNotice ok">{settingsMessage}</div>
                  ) : desktopConnection?.chatgptLogin ? (
                    <div className="settingsNotice">
                      Visit {desktopConnection.chatgptLogin.url} and enter{" "}
                      <code>{desktopConnection.chatgptLogin.code}</code>
                    </div>
                  ) : null}
                  <div className="settingsActions">
                    <button
                      type="submit"
                      className="settingsSave"
                      disabled={settingsSaving}
                    >
                      {settingsSaving ? (
                        <Loader2 size={14} className="spin" />
                      ) : (
                        <Save size={14} />
                      )}
                      Save
                    </button>
                    <button
                      type="button"
                      className="settingsRestart"
                      onClick={() => void restartDesktopBackend()}
                    >
                      <RefreshCw size={14} />
                      Restart
                    </button>
                    {settingsDraft.authMode === "chatgpt" ? (
                      <button
                        type="button"
                        className="settingsRestart"
                        onClick={() => void clearChatgptAuth()}
                        disabled={settingsSaving}
                      >
                        <KeyRound size={14} />
                        Clear Login
                      </button>
                    ) : null}
                  </div>
                </form>
              ) : (
                <label className="endpointField">
                  <span>AG-UI endpoint</span>
                  <input
                    value={baseUrl}
                    onChange={(event) => setBaseUrl(event.target.value)}
                    spellCheck={false}
                  />
                </label>
              )}
            </div>
          ) : null}
        </div>
      </aside>

      <section className="chat" aria-label="Conversation">
        <header className="chatHeader">
          <div className="chatTitleBlock">
            <div className="chatTitle">{threadTitle(activeThread)}</div>
            <div className="chatStatus">
              {running ? <Loader2 size={13} className="spin" /> : null}
              <span>{statusLabel}</span>
              {autoApprove ? (
                <button
                  type="button"
                  className="autoPill"
                  title="Stop auto-approving tools"
                  onClick={() => setAutoApprove(false)}
                >
                  <ShieldAlert size={12} />
                  Auto-approving
                  <X size={12} />
                </button>
              ) : null}
            </div>
          </div>
          <div className="chatActions">
            <button
              className="iconBtn"
              type="button"
              title="Event viewer (⌘⇧E)"
              onClick={openEventViewer}
            >
              <Bug size={16} />
            </button>
            <button
              className="iconBtn"
              type="button"
              title="Refresh"
              onClick={() => void refresh()}
            >
              <RefreshCw size={16} />
            </button>
            {running ? (
              <button
                className="iconBtn danger"
                type="button"
                title="Stop run"
                onClick={() => void stop()}
              >
                <CircleStop size={16} />
              </button>
            ) : canResume ? (
              <button
                className="iconBtn accent"
                type="button"
                title="Resume run"
                onClick={resume}
              >
                <Play size={16} />
              </button>
            ) : null}
          </div>
        </header>

        <div className="transcript" ref={transcriptRef} onScroll={onTranscriptScroll}>
          {timelineItems.length ? (
            <div className="thread">
              {timelineItems.map((item) => (
                <TranscriptItem
                  key={item.id}
                  item={item}
                  approval={
                    item.approvalId
                      ? approvals.find((a) => a.approvalId === item.approvalId)
                      : undefined
                  }
                  running={running}
                  autoApprove={autoApprove}
                  onDecide={decide}
                  onAllowAll={() => setAutoApprove(true)}
                />
              ))}
              {error ? (
                <div className="errorMsg">
                  <AlertTriangle size={16} />
                  <span>{error}</span>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="empty">
              <div className="emptyMark">K</div>
              <div className="emptyTitle">Ask Knuth anything</div>
              <p className="emptyText">
                A literate agent over your codebase — it reasons in the open,
                runs tools, and asks before anything risky.
              </p>
              <div className="examples">
                {EXAMPLE_PROMPTS.map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    className="exampleChip"
                    disabled={backendUnavailable}
                    onClick={() => void sendMessage(prompt)}
                  >
                    {prompt}
                  </button>
                ))}
              </div>
              {error ? (
                <div className="errorMsg" style={{ marginTop: 18 }}>
                  <AlertTriangle size={16} />
                  <span>{error}</span>
                </div>
              ) : null}
            </div>
          )}
        </div>

        <form className="composer" onSubmit={submit}>
          <div className="composerInner">
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={
                backendNeedsSettings
                  ? "Configure model settings…"
                  : desktopBackendStarting
                    ? "Starting backend…"
                    : "Message Knuth…"
              }
              rows={1}
              disabled={backendUnavailable}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            {running ? (
              <button
                className="stop"
                type="button"
                title="Stop"
                onClick={() => void stop()}
              >
                <Square size={15} />
              </button>
            ) : (
              <button
                className="send"
                type="submit"
                title="Send"
                disabled={!draft.trim() || backendUnavailable}
              >
                <ArrowUp size={18} />
              </button>
            )}
          </div>
          <div className="composerHint">
            Enter to send · Shift + Enter for a new line
          </div>
        </form>
      </section>
    </div>
  );
}

function TranscriptItem({
  item,
  approval,
  running,
  autoApprove,
  onDecide,
  onAllowAll,
}: {
  item: TimelineItem;
  approval: ApprovalView | undefined;
  running: boolean;
  autoApprove: boolean;
  onDecide: (approval: ApprovalView, decision: "approved" | "denied") => void;
  onAllowAll: () => void;
}) {
  switch (item.kind) {
    case "user":
      return (
        <div className="turn user">
          <div className="msgRole">
            <span className="roleIcon">
              <User size={13} />
            </span>
            You
          </div>
          <div className="bubble">{item.body}</div>
        </div>
      );

    case "assistant":
      return (
        <div className="turn assistant">
          <div className="msgRole">
            <span className="roleIcon">
              <Sparkles size={13} />
            </span>
            Knuth
          </div>
          {item.body ? (
            <MarkdownView text={item.body} />
          ) : item.status === "running" ? (
            <div className="typing">
              <span />
              <span />
              <span />
            </div>
          ) : null}
        </div>
      );

    case "thinking": {
      const isRunning = item.status === "running";
      return (
        <div className="turn">
          <details className="think" data-running={isRunning} open={isRunning}>
            <summary>
              <ChevronRight size={14} className="chev" />
              <Brain size={14} />
              <span className={isRunning ? "shimmer" : undefined}>
                {isRunning ? "Thinking…" : "Reasoning"}
              </span>
            </summary>
            {item.body ? <div className="thinkBody">{item.body}</div> : null}
          </details>
        </div>
      );
    }

    case "tool":
      return (
        <div className="turn">
          <ToolCard item={item} />
        </div>
      );

    case "approval":
      return (
        <div className="turn">
          <ApprovalCard
            item={item}
            approval={approval}
            running={running}
            autoApprove={autoApprove}
            onDecide={onDecide}
            onAllowAll={onAllowAll}
          />
        </div>
      );

    case "error":
      return (
        <div className="turn">
          <div className="errorMsg">
            <AlertTriangle size={16} />
            <span>{item.body}</span>
          </div>
        </div>
      );

    case "context":
      return (
        <div className="turn">
          <details className="think">
            <summary>
              <ChevronRight size={14} className="chev" />
              <span>{item.title}</span>
            </summary>
            {item.body ? <div className="thinkBody">{item.body}</div> : null}
          </details>
        </div>
      );

    case "run":
      return (
        <div className="turn">
          <div className="divider">
            {item.title}
            {item.timestamp ? ` · ${item.timestamp}` : ""}
          </div>
        </div>
      );
  }
}

function statusChip(status: TimelineStatus, label: string): ReactNode {
  const showSpinner = status === "running" || status === "queued";
  return (
    <span className={`chip ${status}`}>
      {showSpinner ? <Loader2 size={11} className="spin" /> : null}
      {label}
    </span>
  );
}

type ToolPresentation = {
  kicker: string;
  glyph: LucideIcon;
  target?: string;
};

function parseArgs(raw?: string): Record<string, unknown> {
  if (!raw) {
    return {};
  }
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function asString(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  return value == null ? "" : String(value);
}

const PROCESS_RE =
  /^<process_output>\n<stdout>([\s\S]*?)<\/stdout>\n<stderr>([\s\S]*?)<\/stderr>\n<return_code>(-?\d+)<\/return_code>\n<offload>([\s\S]*?)<\/offload>\n<\/process_output>$/;

function unescapeXml(value: string): string {
  return value.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}

function parseProcessOutput(
  result: string,
): { stdout: string; stderr: string; code: number } | null {
  const match = PROCESS_RE.exec(result.trim());
  if (!match) {
    return null;
  }
  return {
    stdout: unescapeXml(match[1]),
    stderr: unescapeXml(match[2]),
    code: Number(match[3]),
  };
}

function toolPresentation(
  name: string,
  args: Record<string, unknown>,
): ToolPresentation {
  switch (name) {
    case "shell":
      return { kicker: "Ran command", glyph: Terminal, target: asString(args.command) };
    case "python":
      return {
        kicker: "Ran Python",
        glyph: Terminal,
        target: asString(args.command || args.code),
      };
    case "read_file":
      return { kicker: "Read file", glyph: FileText, target: asString(args.path) };
    case "write_file":
      return { kicker: "Wrote file", glyph: FilePlus, target: asString(args.path) };
    case "edit_file":
      return { kicker: "Edited file", glyph: FilePen, target: asString(args.path) };
    case "grep":
      return { kicker: "Searched", glyph: Search, target: asString(args.pattern) };
    case "glob":
      return { kicker: "Listed files", glyph: FolderSearch, target: asString(args.pattern) };
    default:
      return { kicker: "Tool", glyph: Terminal, target: name };
  }
}

function ToolCard({ item }: { item: TimelineItem }) {
  const name = item.title;
  const args = parseArgs(item.args);
  const result = item.result?.trim() ?? "";
  const present = toolPresentation(name, args);
  const Glyph = present.glyph;
  const target = present.target?.trim() || name;

  return (
    <div className="tool">
      <div className="toolHead">
        <span className="toolGlyph">
          <Glyph size={15} />
        </span>
        <span className="toolHeadText">
          <span className="toolKicker">{present.kicker}</span>
          <span className="toolTarget">{target}</span>
        </span>
        {statusChip(item.status, item.label)}
      </div>
      <ToolBody name={name} args={args} result={result} status={item.status} />
    </div>
  );
}

function ToolBody({
  name,
  args,
  result,
  status,
}: {
  name: string;
  args: Record<string, unknown>;
  result: string;
  status: TimelineStatus;
}) {
  if (status === "running" && !result) {
    return null;
  }

  if ((name === "shell" || name === "python") && result) {
    const proc = parseProcessOutput(result);
    if (proc) {
      const empty = !proc.stdout.trim() && !proc.stderr.trim();
      return (
        <div className="toolBody">
          <span className={proc.code === 0 ? "exitChip ok" : "exitChip bad"}>
            exit {proc.code}
          </span>
          {proc.stdout.trim() ? (
            <pre className="io">{proc.stdout.replace(/\n$/, "")}</pre>
          ) : null}
          {proc.stderr.trim() ? (
            <pre className="io err">{proc.stderr.replace(/\n$/, "")}</pre>
          ) : null}
          {empty ? <div className="toolNote">No output</div> : null}
        </div>
      );
    }
  }

  if (name === "read_file" && result) {
    const lines = result.split("\n");
    const body = lines[0]?.startsWith("File(") ? lines.slice(1).join("\n") : result;
    return (
      <div className="toolBody">
        <pre className="io fileView">{body}</pre>
      </div>
    );
  }

  if (name === "write_file") {
    const content = asString(args.content);
    return (
      <div className="toolBody">
        {content ? (
          <pre className="io diffAdd">{content}</pre>
        ) : result ? (
          <div className="toolNote">{result}</div>
        ) : null}
      </div>
    );
  }

  if (name === "edit_file") {
    const oldStr = asString(args.old_string);
    const newStr = asString(args.new_string);
    if (oldStr || newStr) {
      return (
        <div className="toolBody">
          <pre className="io diff">
            {oldStr.split("\n").map((line, i) => (
              <div key={`d${i}`} className="diffLine del">{`- ${line}`}</div>
            ))}
            {newStr.split("\n").map((line, i) => (
              <div key={`a${i}`} className="diffLine add">{`+ ${line}`}</div>
            ))}
          </pre>
          {result ? <div className="toolNote">{result}</div> : null}
        </div>
      );
    }
  }

  if ((name === "grep" || name === "glob") && result) {
    return (
      <div className="toolBody">
        <pre className="io">{result}</pre>
      </div>
    );
  }

  const argText = Object.keys(args).length ? JSON.stringify(args, null, 2) : "";
  if (!argText && !result) {
    return null;
  }
  return (
    <div className="toolBody">
      {result ? (
        <details className="toolField" open>
          <summary>Result</summary>
          <pre className="io">{formatJsonish(result)}</pre>
        </details>
      ) : null}
      {argText ? (
        <details className="toolField" open={!result}>
          <summary>Arguments</summary>
          <pre className="io">{argText}</pre>
        </details>
      ) : null}
    </div>
  );
}

function ApprovalCard({
  item,
  approval,
  running,
  autoApprove,
  onDecide,
  onAllowAll,
}: {
  item: TimelineItem;
  approval: ApprovalView | undefined;
  running: boolean;
  autoApprove: boolean;
  onDecide: (approval: ApprovalView, decision: "approved" | "denied") => void;
  onAllowAll: () => void;
}) {
  const pending = Boolean(approval) && item.status === "waiting" && !autoApprove;
  return (
    <div className="approval">
      <div className="approvalHead">
        <ShieldAlert size={15} />
        Approval required
      </div>
      <div className="approvalTitle">{item.title}</div>
      {item.body && item.body !== item.title ? (
        <div className="approvalReason">{item.body}</div>
      ) : null}
      {approval?.risk ? <span className="riskBadge">{approval.risk}</span> : null}
      {item.args && item.args.trim() ? (
        <details className="approvalPreview">
          <summary>Preview</summary>
          <pre className="io">{formatJsonish(item.args)}</pre>
        </details>
      ) : null}
      {pending && approval ? (
        <div className="approvalActions">
          <button
            type="button"
            className="btnApprove"
            disabled={running}
            onClick={() => onDecide(approval, "approved")}
          >
            <Check size={16} />
            Approve
          </button>
          <button
            type="button"
            className="btnAll"
            disabled={running}
            title="Approve this and auto-approve the rest of this conversation"
            onClick={onAllowAll}
          >
            <Check size={16} />
            Approve all
          </button>
          <button
            type="button"
            className="btnDeny"
            disabled={running}
            onClick={() => onDecide(approval, "denied")}
          >
            <X size={16} />
            Deny
          </button>
        </div>
      ) : (
        <div
          className={
            item.status === "denied" ? "resolvedNote denied" : "resolvedNote approved"
          }
        >
          {item.status === "denied" ? <X size={15} /> : <Check size={15} />}
          {item.status === "denied"
            ? "Denied"
            : item.status === "approved"
              ? "Approved"
              : "Resolved"}
        </div>
      )}
    </div>
  );
}
