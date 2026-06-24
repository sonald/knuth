const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const CONFIG_FILE = "config.json";
const SECRETS_FILE = "secrets.json";
const CONFIG_VERSION = 1;
const DEFAULT_TIMEOUT = 60;
const MAX_TIMEOUT = 3600;

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function asString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return {};
    }
    throw error;
  }
}

function writeJsonAtomic(filePath, value, mode) {
  ensureDir(path.dirname(filePath));
  const tempPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tempPath, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode,
  });
  fs.renameSync(tempPath, filePath);
  if (mode !== undefined) {
    fs.chmodSync(filePath, mode);
  }
}

function defaultWorkspace(app, appRoot) {
  if (app.isPackaged) {
    return os.homedir();
  }
  return path.resolve(appRoot, "../..");
}

function defaultDbPath(app) {
  return path.join(app.getPath("userData"), "knuth-im.db");
}

function normalizeUrl(value) {
  const raw = asString(value);
  if (!raw) {
    return "";
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("Model base URL must be a valid URL.");
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("Model base URL must use http or https.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Model base URL must not contain credentials.");
  }
  parsed.hash = "";
  return parsed.toString().replace(/\/$/, "");
}

function normalizeModel(value) {
  const model = asString(value);
  if (!model) {
    return "";
  }
  if (model.length > 200 || /[\r\n\t]/.test(model)) {
    throw new Error("Model name is invalid.");
  }
  return model;
}

function normalizeAuthMode(value, model) {
  const mode = asString(value);
  if (mode) {
    if (!["api_key", "chatgpt"].includes(mode)) {
      throw new Error("Auth mode is invalid.");
    }
    return mode;
  }
  return normalizeModel(model).startsWith("chatgpt/") ? "chatgpt" : "api_key";
}

function normalizeTimeout(value) {
  if (value === undefined || value === null || value === "") {
    return DEFAULT_TIMEOUT;
  }
  const timeout = Number(value);
  if (!Number.isFinite(timeout) || timeout <= 0 || timeout > MAX_TIMEOUT) {
    throw new Error(`Timeout must be between 0 and ${MAX_TIMEOUT} seconds.`);
  }
  return timeout;
}

function normalizeExistingDirectory(value, fallback) {
  const raw = asString(value) || fallback;
  const resolved = path.resolve(raw);
  let stat;
  try {
    stat = fs.statSync(resolved);
  } catch {
    throw new Error(`Workspace directory does not exist: ${resolved}`);
  }
  if (!stat.isDirectory()) {
    throw new Error(`Workspace must be a directory: ${resolved}`);
  }
  return resolved;
}

function normalizeDbPath(value, fallback) {
  const raw = asString(value) || fallback;
  const resolved = path.resolve(raw);
  const parent = path.dirname(resolved);
  let stat;
  try {
    stat = fs.statSync(parent);
  } catch {
    throw new Error(`Database directory does not exist: ${parent}`);
  }
  if (!stat.isDirectory()) {
    throw new Error(`Database parent must be a directory: ${parent}`);
  }
  return resolved;
}

function hasPlainApiKey(secrets) {
  return Boolean(
    isRecord(secrets) &&
      isRecord(secrets.apiKey) &&
      typeof secrets.apiKey.value === "string" &&
      secrets.apiKey.value.length > 0,
  );
}

class SettingsStore {
  constructor({ app, appRoot }) {
    this.app = app;
    this.appRoot = appRoot;
  }

  configPath() {
    return path.join(this.app.getPath("userData"), CONFIG_FILE);
  }

  secretsPath() {
    return path.join(this.app.getPath("userData"), SECRETS_FILE);
  }

  chatgptTokenDir() {
    const dirPath = path.join(this.app.getPath("userData"), "litellm-chatgpt");
    ensureDir(dirPath);
    fs.chmodSync(dirPath, 0o700);
    return dirPath;
  }

  chatgptAuthPath() {
    return path.join(this.chatgptTokenDir(), "auth.json");
  }

  hasChatgptAuth() {
    return fs.existsSync(this.chatgptAuthPath());
  }

  clearChatgptAuth() {
    fs.rmSync(this.chatgptTokenDir(), { recursive: true, force: true });
  }

  readConfig() {
    const config = readJson(this.configPath());
    return isRecord(config) ? config : {};
  }

  readSecrets() {
    const secrets = readJson(this.secretsPath());
    return isRecord(secrets) ? secrets : {};
  }

  hasStoredApiKey() {
    return hasPlainApiKey(this.readSecrets());
  }

  readApiKey() {
    const secrets = this.readSecrets();
    if (!hasPlainApiKey(secrets)) {
      return "";
    }
    return secrets.apiKey.value;
  }

  writeApiKey(apiKey) {
    writeJsonAtomic(
      this.secretsPath(),
      {
        version: CONFIG_VERSION,
        apiKey: {
          value: apiKey,
          storage: "local-file",
        },
      },
      0o600,
    );
  }

  clearApiKey() {
    writeJsonAtomic(
      this.secretsPath(),
      {
        version: CONFIG_VERSION,
      },
      0o600,
    );
  }

  save(input) {
    if (!isRecord(input)) {
      throw new Error("Settings payload must be an object.");
    }

    const previous = this.readConfig();
    const fallbackWorkspace = defaultWorkspace(this.app, this.appRoot);
    const fallbackDbPath = defaultDbPath(this.app);
    const next = {
      version: CONFIG_VERSION,
      authMode:
        "authMode" in input
          ? normalizeAuthMode(input.authMode, input.model ?? previous.model)
          : normalizeAuthMode(previous.authMode, input.model ?? previous.model),
      modelBaseUrl:
        "modelBaseUrl" in input
          ? normalizeUrl(input.modelBaseUrl)
          : asString(previous.modelBaseUrl),
      model:
        "model" in input ? normalizeModel(input.model) : asString(previous.model),
      timeout:
        "timeout" in input
          ? normalizeTimeout(input.timeout)
          : normalizeTimeout(previous.timeout),
      workspace:
        "workspace" in input
          ? normalizeExistingDirectory(input.workspace, fallbackWorkspace)
          : normalizeExistingDirectory(previous.workspace, fallbackWorkspace),
      dbPath:
        "dbPath" in input
          ? normalizeDbPath(input.dbPath, fallbackDbPath)
          : normalizeDbPath(previous.dbPath, fallbackDbPath),
    };
    const apiKey = "apiKey" in input ? asString(input.apiKey) : "";

    writeJsonAtomic(this.configPath(), next, 0o600);

    if (input.clearApiKey === true) {
      this.clearApiKey();
    }
    if (apiKey) {
      this.writeApiKey(apiKey);
    }

    return this.publicSettings(process.env, { allowEnvFallback: !this.app.isPackaged });
  }

  publicSettings(env = process.env, { allowEnvFallback = true } = {}) {
    const config = this.readConfig();
    const envWorkspace = allowEnvFallback ? asString(env.KNUTH_IM_WORKSPACE) : "";
    const envDbPath = allowEnvFallback ? asString(env.KNUTH_IM_DB_PATH) : "";
    const storedApiKey = this.hasStoredApiKey();
    const envApiKey = allowEnvFallback && Boolean(asString(env.KNUTH_API_KEY));
    const workspaceFallback = envWorkspace || defaultWorkspace(this.app, this.appRoot);
    const dbPathFallback = envDbPath || defaultDbPath(this.app);

    let workspace = "";
    let workspaceValid = true;
    try {
      workspace = normalizeExistingDirectory(config.workspace, workspaceFallback);
    } catch {
      workspace = asString(config.workspace) || workspaceFallback;
      workspaceValid = false;
    }

    let dbPath = "";
    let dbPathValid = true;
    try {
      dbPath = normalizeDbPath(config.dbPath, dbPathFallback);
    } catch {
      dbPath = asString(config.dbPath) || dbPathFallback;
      dbPathValid = false;
    }

    const model =
      asString(config.model) ||
      (allowEnvFallback ? asString(env.KNUTH_MODEL) : "");
    const authMode = normalizeAuthMode(config.authMode || env.KNUTH_AUTH_MODE, model);
    const modelBaseUrl =
      authMode === "chatgpt"
        ? ""
        :
        asString(config.modelBaseUrl) ||
          (allowEnvFallback ? asString(env.KNUTH_BASE_URL) : "");
    const settings = {
      authMode,
      modelBaseUrl,
      model,
      timeout:
        config.timeout !== undefined
          ? normalizeTimeout(config.timeout)
          : normalizeTimeout(allowEnvFallback ? env.KNUTH_TIMEOUT : undefined),
      workspace,
      dbPath,
      hasApiKey: authMode === "api_key" && (storedApiKey || envApiKey),
      apiKeySource:
        authMode === "api_key"
          ? storedApiKey
            ? "stored"
            : envApiKey
              ? "environment"
              : null
          : null,
      secretStorage: storedApiKey ? "local-file" : null,
      hasChatgptAuth: authMode === "chatgpt" && this.hasChatgptAuth(),
      missing: [],
      ready: false,
    };
    settings.needsLogin = authMode === "chatgpt" && !settings.hasChatgptAuth;

    const missing = [];
    if (authMode === "api_key" && !settings.modelBaseUrl) {
      missing.push("modelBaseUrl");
    }
    if (!settings.model) {
      missing.push("model");
    }
    if (authMode === "api_key" && !settings.hasApiKey) {
      missing.push("apiKey");
    }
    if (!workspaceValid) {
      missing.push("workspace");
    }
    if (!dbPathValid) {
      missing.push("dbPath");
    }
    settings.missing = missing;
    settings.ready = missing.length === 0;
    return settings;
  }

  resolveForBackend(env = process.env, { allowEnvFallback = true } = {}) {
    const publicSettings = this.publicSettings(env, { allowEnvFallback });
    let apiKey = "";
    if (publicSettings.apiKeySource === "stored") {
      apiKey = this.readApiKey();
    } else if (allowEnvFallback) {
      apiKey = asString(env.KNUTH_API_KEY);
    }
    return {
      ...publicSettings,
      apiKey,
      chatgptTokenDir:
        publicSettings.authMode === "chatgpt" ? this.chatgptTokenDir() : "",
    };
  }
}

module.exports = {
  SettingsStore,
  normalizeUrl,
  normalizeTimeout,
};
