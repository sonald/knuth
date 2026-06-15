import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const appRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const startUrl = process.env.ELECTRON_START_URL || "http://127.0.0.1:3000";
const isWindows = process.platform === "win32";

async function isReachable(url) {
  try {
    const response = await fetch(url, { method: "HEAD" });
    return response.ok || response.status < 500;
  } catch {
    return false;
  }
}

async function waitForUrl(url, timeoutMs = 45_000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await isReachable(url)) {
      return;
    }
    await delay(500);
  }
  throw new Error(`Timed out waiting for ${url}`);
}

function spawnCommand(command, args, options = {}) {
  return spawn(command, args, {
    cwd: appRoot,
    stdio: "inherit",
    shell: isWindows,
    ...options,
  });
}

let nextProcess = null;
if (!(await isReachable(startUrl))) {
  nextProcess = spawnCommand("npm", [
    "run",
    "dev",
    "--",
    "--hostname",
    "127.0.0.1",
    "--port",
    "3000",
  ]);
  await waitForUrl(startUrl);
}

const electronBin = path.join(
  appRoot,
  "node_modules",
  ".bin",
  isWindows ? "electron.cmd" : "electron",
);

const electronProcess = spawnCommand(electronBin, [appRoot], {
  env: {
    ...process.env,
    ELECTRON_START_URL: startUrl,
  },
});

function stop() {
  if (nextProcess !== null && !nextProcess.killed) {
    nextProcess.kill("SIGTERM");
  }
}

electronProcess.on("exit", (code) => {
  stop();
  process.exit(code ?? 0);
});

process.on("SIGINT", () => {
  stop();
  electronProcess.kill("SIGINT");
});

process.on("SIGTERM", () => {
  stop();
  electronProcess.kill("SIGTERM");
});
