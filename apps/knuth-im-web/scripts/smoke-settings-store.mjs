import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import backendModule from "../electron/backend-manager.cjs";
import settingsModule from "../electron/settings-store.cjs";

const { BackendManager } = backendModule;
const { SettingsStore } = settingsModule;

const appRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "knuth-im-settings-"));
const userData = path.join(tempRoot, "user-data");
const workspace = path.join(tempRoot, "workspace");
fs.mkdirSync(userData, { recursive: true });
fs.mkdirSync(workspace, { recursive: true });

function fakeApp(isPackaged = true, dataDir = userData) {
  return {
    isPackaged,
    getPath(name) {
      if (name !== "userData") {
        throw new Error(`Unexpected app path: ${name}`);
      }
      return dataDir;
    },
  };
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

try {
  const store = new SettingsStore({
    app: fakeApp(),
    appRoot,
  });
  const before = store.publicSettings({}, { allowEnvFallback: false });
  assert(before.ready === false, "fresh store should not be ready");
  assert(before.missing.includes("apiKey"), "fresh store should require api key");

  const saved = store.save({
    modelBaseUrl: "https://models.example.test/v1",
    model: "knuth-test-model",
    timeout: "42",
    workspace,
    dbPath: path.join(userData, "knuth-im.db"),
    apiKey: "secret-test-key",
  });
  assert(saved.ready === true, "saved settings should be ready");
  assert(saved.hasApiKey === true, "saved settings should report an API key");
  assert(!("apiKey" in saved), "public settings must not include the API key");
  assert(store.readApiKey() === "secret-test-key", "stored API key should be readable by main");
  const secrets = JSON.parse(fs.readFileSync(path.join(userData, "secrets.json"), "utf8"));
  assert(secrets.apiKey.storage === "local-file", "API key should use local file storage");
  assert(secrets.apiKey.value === "secret-test-key", "API key should be stored in the local secret file");
  const secretMode = fs.statSync(path.join(userData, "secrets.json")).mode & 0o777;
  assert(secretMode === 0o600, `secrets.json should be mode 0600, got ${secretMode.toString(8)}`);

  let rejectedInvalidUrl = false;
  try {
    store.save({ modelBaseUrl: "file:///tmp/model" });
  } catch {
    rejectedInvalidUrl = true;
  }
  assert(rejectedInvalidUrl, "non-http model URLs should be rejected");

  const missingUserData = path.join(tempRoot, "missing-user-data");
  fs.mkdirSync(missingUserData, { recursive: true });
  const missingStore = new SettingsStore({
    app: fakeApp(true, missingUserData),
    appRoot,
  });
  const manager = new BackendManager({
    app: fakeApp(true, missingUserData),
    appRoot,
    settingsStore: missingStore,
    logger: { info() {}, warn() {} },
  });
  const state = await manager.start();
  assert(state.status === "needs_settings", "missing packaged settings should not start the sidecar");
  assert(state.settings.missing.includes("apiKey"), "missing state should name apiKey");

  const backendUserData = path.join(tempRoot, "backend-user-data");
  fs.mkdirSync(backendUserData, { recursive: true });
  const backendStore = new SettingsStore({
    app: fakeApp(true, backendUserData),
    appRoot,
  });
  backendStore.save({
    modelBaseUrl: "http://127.0.0.1:9/v1",
    model: "knuth-test-model",
    workspace,
    dbPath: path.join(backendUserData, "knuth-im.db"),
    apiKey: "backend-manager-smoke-key",
  });
  const previousEnv = {
    command: process.env.KNUTH_IM_BACKEND_COMMAND,
    args: process.env.KNUTH_IM_BACKEND_ARGS,
    cwd: process.env.KNUTH_IM_BACKEND_CWD,
  };
  const executable = process.platform === "win32" ? "knuth-im.exe" : "knuth-im";
  const binaryCandidates = [
    path.join(appRoot, "sidecar", "knuth-im", executable),
    path.join(appRoot, "sidecar", executable),
  ];
  const binaryPath = binaryCandidates.find((candidate) => fs.existsSync(candidate));
  if (binaryPath) {
    process.env.KNUTH_IM_BACKEND_COMMAND = binaryPath;
    process.env.KNUTH_IM_BACKEND_ARGS = "[]";
    process.env.KNUTH_IM_BACKEND_CWD = workspace;
    const readyManager = new BackendManager({
      app: fakeApp(true, backendUserData),
      appRoot,
      settingsStore: backendStore,
      logger: { info() {}, warn() {} },
    });
    const readyState = await readyManager.start();
    try {
      assert(readyState.status === "ready", `saved settings should start sidecar, got ${readyState.status}`);
      assert(readyState.settings.hasApiKey === true, "ready state should report saved API key");
      assert(!("apiKey" in readyState.settings), "ready state public settings must not include API key");
    } finally {
      readyManager.stop();
      if (previousEnv.command === undefined) {
        delete process.env.KNUTH_IM_BACKEND_COMMAND;
      } else {
        process.env.KNUTH_IM_BACKEND_COMMAND = previousEnv.command;
      }
      if (previousEnv.args === undefined) {
        delete process.env.KNUTH_IM_BACKEND_ARGS;
      } else {
        process.env.KNUTH_IM_BACKEND_ARGS = previousEnv.args;
      }
      if (previousEnv.cwd === undefined) {
        delete process.env.KNUTH_IM_BACKEND_CWD;
      } else {
        process.env.KNUTH_IM_BACKEND_CWD = previousEnv.cwd;
      }
    }
  }

  console.log("Settings store smoke passed");
} finally {
  fs.rmSync(tempRoot, { recursive: true, force: true });
}
