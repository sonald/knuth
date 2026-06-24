import type { AgentConnection } from "../lib/agui";

export type KnuthDesktopSettings = {
  authMode: "api_key" | "chatgpt";
  modelBaseUrl: string;
  model: string;
  timeout: number;
  workspace: string;
  dbPath: string;
  hasApiKey: boolean;
  hasChatgptAuth?: boolean;
  needsLogin?: boolean;
  apiKeySource: "stored" | "environment" | null;
  secretStorage?: "local-file" | null;
  missing: string[];
  ready: boolean;
};

export type KnuthDesktopSettingsInput = {
  authMode?: "api_key" | "chatgpt";
  modelBaseUrl?: string;
  model?: string;
  timeout?: number | string;
  workspace?: string;
  dbPath?: string;
  apiKey?: string;
  clearApiKey?: boolean;
};

declare global {
  interface Window {
    knuthDesktop?: {
      platform: string;
      versions: {
        electron: string;
        chrome: string;
        node: string;
      };
      backend?: () => Promise<AgentConnection>;
      restartBackend?: () => Promise<AgentConnection>;
      getSettings?: () => Promise<KnuthDesktopSettings>;
      saveSettings?: (
        settings: KnuthDesktopSettingsInput,
      ) => Promise<{ settings: KnuthDesktopSettings; backend: AgentConnection }>;
      clearChatgptAuth?: () => Promise<{
        settings: KnuthDesktopSettings;
        backend: AgentConnection;
      }>;
      chooseWorkspace?: () => Promise<string | null>;
      openEventViewer?: () => Promise<void>;
    };
  }
}

export {};
