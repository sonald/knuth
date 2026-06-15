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
const envPath = path.join(repoRoot, ".env");

function parseDotEnv(filePath) {
  const parsed = {};
  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    const normalized = line.startsWith("export ") ? line.slice("export ".length).trim() : line;
    const equals = normalized.indexOf("=");
    if (equals <= 0) {
      continue;
    }
    const key = normalized.slice(0, equals).trim();
    let value = normalized.slice(equals + 1).trim();
    if (
      (value.startsWith("\"") && value.endsWith("\"")) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    parsed[key] = value;
  }
  return parsed;
}

function requireSetting(env, key) {
  const value = env[key];
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`Missing required ${key} in ${envPath}`);
  }
  return value.trim();
}

function stop(child) {
  if (!child.killed) {
    child.kill("SIGTERM");
  }
}

function redact(value) {
  return value
    .replaceAll(requireSetting(envConfig, "KNUTH_API_KEY"), "[REDACTED_API_KEY]")
    .replace(/(authorization:\s*Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]");
}

if (!fs.existsSync(envPath)) {
  throw new Error(`Root .env not found at ${envPath}`);
}

const envConfig = parseDotEnv(envPath);
requireSetting(envConfig, "KNUTH_API_KEY");
requireSetting(envConfig, "KNUTH_BASE_URL");
requireSetting(envConfig, "KNUTH_MODEL");

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

const host = "127.0.0.1";
const port = await getFreePort(host);
const baseUrl = `http://${host}:${port}`;
const token = crypto.randomBytes(32).toString("hex");
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "knuth-im-real-env-"));
const dbPath = path.join(tempRoot, "knuth-im.db");
const runId = `run_real_env_smoke_${Date.now()}`;

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
    repoRoot,
    "--env-file",
    envPath,
  ],
  {
    cwd: repoRoot,
    env: {
      ...process.env,
      ...envConfig,
      KNUTH_IM_AUTH_TOKEN: token,
      KNUTH_TIMEOUT: process.env.KNUTH_TIMEOUT || envConfig.KNUTH_TIMEOUT || "60",
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

try {
  await waitForHealth(baseUrl, 120_000);

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120_000);
  const response = await fetch(`${baseUrl}/agent`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      threadId: runId,
      runId,
      messages: [
        {
          role: "user",
          content: "For this smoke test, do not call tools. Reply with one short greeting.",
        },
      ],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {},
    }),
    signal: controller.signal,
  }).finally(() => clearTimeout(timeout));

  const body = await response.text();
  if (!response.ok) {
    throw new Error(`Expected /agent to stream, got ${response.status}: ${body}`);
  }
  if (body.includes("RUN_ERROR")) {
    throw new Error(`Real model smoke returned RUN_ERROR:\n${body}`);
  }
  if (!body.includes("\"type\":\"RUN_FINISHED\"")) {
    throw new Error(`Real model smoke did not finish:\n${body}`);
  }
  if (!body.includes("\"type\":\"TEXT_MESSAGE_CONTENT\"")) {
    throw new Error(`Real model smoke did not stream assistant text:\n${body}`);
  }

  console.log(`Real sidecar model smoke passed at ${baseUrl}`);
} catch (error) {
  console.error(redact(error instanceof Error ? error.message : String(error)));
  if (stdout.trim()) {
    console.error("sidecar stdout:");
    console.error(redact(stdout.trim()));
  }
  if (stderr.trim()) {
    console.error("sidecar stderr:");
    console.error(redact(stderr.trim()));
  }
  process.exitCode = 1;
} finally {
  stop(child);
  fs.rmSync(tempRoot, { recursive: true, force: true });
}
