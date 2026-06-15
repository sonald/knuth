import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import backendManager from "../electron/backend-manager.cjs";

const { getFreePort, waitForHealth } = backendManager;

const appRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repoRoot = path.resolve(appRoot, "../..");
const host = "127.0.0.1";
const port = await getFreePort(host);
const baseUrl = `http://${host}:${port}`;
const token = crypto.randomBytes(32).toString("hex");
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "knuth-im-sidecar-"));
const workspace = path.join(tempRoot, "workspace");
const dbPath = path.join(tempRoot, "knuth-im.db");
fs.mkdirSync(workspace);

const child = spawn(
  "uv",
  [
    "run",
    "knuth-im",
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
    cwd: repoRoot,
    env: {
      ...process.env,
      KNUTH_API_KEY: "sidecar-smoke-key",
      KNUTH_BASE_URL: "https://example.invalid/v1",
      KNUTH_MODEL: "sidecar-smoke-model",
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
  await waitForHealth(baseUrl, 45_000);

  const unauthorized = await fetch(`${baseUrl}/threads`);
  if (unauthorized.status !== 401) {
    throw new Error(`Expected unauthenticated /threads to return 401, got ${unauthorized.status}`);
  }

  const authorized = await fetch(`${baseUrl}/threads`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!authorized.ok) {
    throw new Error(`Expected authenticated /threads to succeed, got ${authorized.status}`);
  }
  const payload = await authorized.json();
  if (!Array.isArray(payload.threads)) {
    throw new Error("Expected /threads response to contain a threads array");
  }

  console.log(`Sidecar smoke passed at ${baseUrl}`);
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
