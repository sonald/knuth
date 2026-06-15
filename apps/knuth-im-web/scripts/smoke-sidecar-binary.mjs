import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import backendManager from "../electron/backend-manager.cjs";

const { getFreePort, waitForHealth } = backendManager;

const appRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const host = "127.0.0.1";
const port = await getFreePort(host);
const baseUrl = `http://${host}:${port}`;
const token = crypto.randomBytes(32).toString("hex");
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "knuth-im-sidecar-binary-"));
const workspace = path.join(tempRoot, "workspace");
const dbPath = path.join(tempRoot, "knuth-im.db");
fs.mkdirSync(workspace);

const executable = process.platform === "win32" ? "knuth-im.exe" : "knuth-im";
const binaryCandidates = [
  path.join(appRoot, "sidecar", "knuth-im", executable),
  path.join(appRoot, "sidecar", executable),
];
const binaryPath = binaryCandidates.find((candidate) => fs.existsSync(candidate));
if (!binaryPath) {
  throw new Error(
    `Sidecar binary not found at ${binaryCandidates.join(" or ")}; run npm run sidecar:build first.`,
  );
}

const child = spawn(
  binaryPath,
  [
    "--host",
    host,
    "--port",
    String(port),
    "--db-path",
    dbPath,
    "--workspace",
    workspace,
  ],
  {
    cwd: workspace,
    env: {
      ...process.env,
      KNUTH_API_KEY: "sidecar-binary-smoke-key",
      KNUTH_BASE_URL: "http://127.0.0.1:9/v1",
      KNUTH_MODEL: "MiniMax-M3",
      KNUTH_TIMEOUT: "3",
      KNUTH_IM_AUTH_TOKEN: token,
    },
    stdio: ["ignore", "pipe", "pipe"],
  },
);

let stdout = "";
let stderr = "";
child.stdout.on("data", (chunk) => {
  stdout += chunk.toString();
});
child.stderr.on("data", (chunk) => {
  stderr += chunk.toString();
});

function stop() {
  if (!child.killed) {
    child.kill("SIGTERM");
  }
}

try {
  await waitForHealth(baseUrl, 120_000);

  const response = await fetch(`${baseUrl}/agent`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      threadId: "run_sidecar_binary_smoke",
      runId: "run_sidecar_binary_smoke",
      messages: [{ role: "user", content: "Say hello once." }],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {},
    }),
  });
  if (!response.ok) {
    throw new Error(`Expected /agent to stream, got ${response.status}: ${await response.text()}`);
  }
  const body = await response.text();
  if (body.includes("model_prices_and_context_window_backup.json")) {
    throw new Error("PyInstaller sidecar is missing LiteLLM model metadata.");
  }
  if (body.includes("FileNotFoundError")) {
    throw new Error(`Unexpected FileNotFoundError from sidecar binary:\n${body}`);
  }
  if (!body.includes("RUN_ERROR")) {
    throw new Error(`Expected controlled model failure from unreachable smoke endpoint, got:\n${body}`);
  }

  console.log(`Sidecar binary smoke passed at ${baseUrl}`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  if (stdout.trim()) {
    console.error("sidecar stdout:");
    console.error(stdout.trim());
  }
  if (stderr.trim()) {
    console.error("sidecar stderr:");
    console.error(stderr.trim());
  }
  process.exitCode = 1;
} finally {
  stop();
  fs.rmSync(tempRoot, { recursive: true, force: true });
}
