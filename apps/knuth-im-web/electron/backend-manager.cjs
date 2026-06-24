const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_EXTERNAL_URL = "http://127.0.0.1:8000";

function randomToken() {
  return crypto.randomBytes(32).toString("hex");
}

function parseJsonArgs(value) {
  if (!value) {
    return [];
  }
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed) || !parsed.every((item) => typeof item === "string")) {
    throw new Error("KNUTH_IM_BACKEND_ARGS must be a JSON string array");
  }
  return parsed;
}

function localWorkspace(app, appRoot) {
  if (process.env.KNUTH_IM_WORKSPACE) {
    return path.resolve(process.env.KNUTH_IM_WORKSPACE);
  }
  if (!app.isPackaged) {
    return path.resolve(appRoot, "../..");
  }
  return os.homedir();
}

function resolveCommand(app, appRoot) {
  if (process.env.KNUTH_IM_BACKEND_COMMAND) {
    return {
      command: process.env.KNUTH_IM_BACKEND_COMMAND,
      args: parseJsonArgs(process.env.KNUTH_IM_BACKEND_ARGS),
      cwd: process.env.KNUTH_IM_BACKEND_CWD || appRoot,
    };
  }

  if (!app.isPackaged) {
    return {
      command: "uv",
      args: ["run", "knuth-im"],
      cwd: path.resolve(appRoot, "../.."),
    };
  }

  const executable = process.platform === "win32" ? "knuth-im.exe" : "knuth-im";
  const resourceRoot = path.join(process.resourcesPath, "knuth-im-sidecar");
  const candidates = [
    path.join(resourceRoot, "knuth-im", executable),
    path.join(resourceRoot, executable),
  ];
  const command = candidates.find((candidate) => fs.existsSync(candidate)) || candidates[0];
  return {
    command,
    args: [],
    cwd: app.getPath("userData"),
  };
}

function getFreePort(host = DEFAULT_HOST) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, host, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        resolve(port);
      });
    });
  });
}

async function waitForHealth(baseUrl, timeoutMs = 120_000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const response = await fetch(`${baseUrl}/healthz`, { cache: "no-store" });
      if (response.ok) {
        return;
      }
    } catch {
      // The sidecar usually needs a moment before uvicorn accepts connections.
    }
    await delay(250);
  }
  throw new Error(`Timed out waiting for backend health at ${baseUrl}/healthz`);
}

function modelEnvironment(settings) {
  const env = {
    KNUTH_AUTH_MODE: settings.authMode || "api_key",
    KNUTH_MODEL: settings.model,
    KNUTH_TIMEOUT: String(settings.timeout),
  };
  if (settings.authMode === "chatgpt") {
    env.CHATGPT_TOKEN_DIR = settings.chatgptTokenDir;
    return env;
  }
  env.KNUTH_API_KEY = settings.apiKey;
  env.KNUTH_BASE_URL = settings.modelBaseUrl;
  return env;
}

function parseChatgptDeviceCode(text) {
  const url = text.match(/Visit\s+(https?:\/\/\S+)/)?.[1];
  const code = text.match(/Enter code:\s*([A-Z0-9-]+)/)?.[1];
  return url && code ? { url, code } : null;
}

class BackendManager {
  constructor({ app, appRoot, logger = console, settingsStore = null }) {
    this.app = app;
    this.appRoot = appRoot;
    this.logger = logger;
    this.settingsStore = settingsStore;
    this.child = null;
    this.outputBuffer = "";
    this.state = {
      status: "starting",
      baseUrl: process.env.NEXT_PUBLIC_KNUTH_AGUI_URL || DEFAULT_EXTERNAL_URL,
      headers: {},
      mode: "sidecar",
    };
  }

  async start() {
    try {
      return await this._start();
    } catch (error) {
      this.state = {
        ...this.state,
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
      };
      this.stop();
      return this.state;
    }
  }

  async _start() {
    const mode = process.env.KNUTH_IM_ELECTRON_BACKEND || "sidecar";
    if (mode === "external") {
      this.state = {
        status: "external",
        baseUrl:
          process.env.NEXT_PUBLIC_KNUTH_AGUI_URL ||
          process.env.KNUTH_AGUI_URL ||
          DEFAULT_EXTERNAL_URL,
        headers: {},
        mode,
      };
      return this.state;
    }

    const settings = this.resolveModelSettings();
    if (!settings.ready) {
      const host = process.env.KNUTH_IM_HOST || DEFAULT_HOST;
      const port = Number(process.env.KNUTH_IM_PORT || 8000);
      const baseUrl = `http://${host}:${port}`;
      this.state = {
        status: "needs_settings",
        baseUrl,
        headers: {},
        mode,
        workspace: settings.workspace,
        settings,
        error: `Missing model settings: ${settings.missing.join(", ")}`,
      };
      return this.state;
    }

    const host = process.env.KNUTH_IM_HOST || DEFAULT_HOST;
    const port = Number(process.env.KNUTH_IM_PORT || (await getFreePort(host)));
    const baseUrl = `http://${host}:${port}`;
    const token = randomToken();
    const workspace = settings.workspace || localWorkspace(this.app, this.appRoot);
    const dbPath =
      settings.dbPath ||
      process.env.KNUTH_IM_DB_PATH ||
      path.join(this.app.getPath("userData"), "knuth-im.db");
    const envFile =
      process.env.KNUTH_IM_ENV_FILE || path.join(workspace, ".env");
    const command = resolveCommand(this.app, this.appRoot);
    const args = [
      ...command.args,
      "--host",
      host,
      "--port",
      String(port),
      "--db-path",
      dbPath,
      "--workspace",
      workspace,
    ];
    if (fs.existsSync(envFile)) {
      args.push("--env-file", envFile);
    }

    this.state = {
      status: "starting",
      baseUrl,
      headers: { Authorization: `Bearer ${token}` },
      mode,
      workspace,
      settings: publicSettings(settings),
    };

    const childEnv = {
      ...process.env,
      ...modelEnvironment(settings),
      KNUTH_IM_AUTH_TOKEN: token,
    };
    if (settings.authMode === "chatgpt") {
      delete childEnv.KNUTH_API_KEY;
      delete childEnv.KNUTH_BASE_URL;
    }

    this.child = spawn(command.command, args, {
      cwd: command.cwd,
      env: childEnv,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });

    this.child.stdout.on("data", (chunk) => {
      this._recordOutput(chunk, "info");
    });
    this.child.stderr.on("data", (chunk) => {
      this._recordOutput(chunk, "warn");
    });
    this.child.on("exit", (code, signal) => {
      if (this.state.status === "ready") {
        this.state = {
          ...this.state,
          status: "stopped",
          error: `Backend exited with code ${code ?? "null"} signal ${
            signal ?? "null"
          }`,
        };
      }
      this.child = null;
    });
    this.child.on("error", (error) => {
      this.state = {
        ...this.state,
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
      };
    });

    try {
      await waitForHealth(baseUrl);
      this.state = { ...this.state, status: "ready" };
    } catch (error) {
      this.state = {
        ...this.state,
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
      };
      this.stop();
    }
    return this.state;
  }

  connection() {
    return this.state;
  }

  resolveModelSettings() {
    if (this.settingsStore) {
      return this.settingsStore.resolveForBackend(process.env, {
        allowEnvFallback: !this.app.isPackaged,
      });
    }

    const missing = [];
    const settings = {
      modelBaseUrl: process.env.KNUTH_BASE_URL || "",
      model: process.env.KNUTH_MODEL || "",
      timeout: Number(process.env.KNUTH_TIMEOUT || 60),
      workspace: localWorkspace(this.app, this.appRoot),
      dbPath:
        process.env.KNUTH_IM_DB_PATH ||
        path.join(this.app.getPath("userData"), "knuth-im.db"),
      hasApiKey: Boolean(process.env.KNUTH_API_KEY),
      apiKeySource: process.env.KNUTH_API_KEY ? "environment" : null,
      apiKey: process.env.KNUTH_API_KEY || "",
      missing,
      ready: false,
    };
    if (!settings.modelBaseUrl) {
      missing.push("modelBaseUrl");
    }
    if (!settings.model) {
      missing.push("model");
    }
    if (!settings.apiKey) {
      missing.push("apiKey");
    }
    settings.ready = missing.length === 0;
    return settings;
  }

  async restart() {
    this.stop();
    await delay(150);
    return this.start();
  }

  stop() {
    if (this.child && !this.child.killed) {
      this.child.kill("SIGTERM");
    }
    this.child = null;
  }

  _recordOutput(chunk, level) {
    const text = chunk.toString();
    this.logger[level](`[knuth-im] ${text.trimEnd()}`);
    this.outputBuffer = `${this.outputBuffer}${text}`.slice(-2000);
    const login = parseChatgptDeviceCode(this.outputBuffer);
    if (login) {
      this.state = {
        ...this.state,
        status: "login_required",
        chatgptLogin: login,
        error: "ChatGPT login required",
      };
    }
  }
}

function publicSettings(settings) {
  const { apiKey: _apiKey, ...rest } = settings;
  return rest;
}

module.exports = {
  BackendManager,
  getFreePort,
  modelEnvironment,
  parseChatgptDeviceCode,
  waitForHealth,
};
