# Electron Backend Manager

Status: done

## Description

Add an Electron main-process backend manager that allocates a localhost port, generates a launch token, starts the backend sidecar, waits for health, exposes connection metadata through preload, and tears down the child process on exit.

## Acceptance Criteria

- [x] Dev mode can spawn `uv run knuth-im` with explicit flags.
- [x] Packaged mode has a clear sidecar executable lookup contract for `extraResources` packaging.
- [x] Renderer receives backend status and connection metadata through preload/IPC.
- [x] Backend child process is terminated when Electron exits.

## Verification

- [x] `cd apps/knuth-im-web && npm run typecheck`
- [x] `cd apps/knuth-im-web && node scripts/smoke-sidecar.mjs`

## Comments

- Implemented `electron/backend-manager.cjs`, preload `backend()` IPC, sidecar port/token generation, health polling, and child teardown.
- Verified with `npm run typecheck`, `npm run smoke:sidecar`, `npm run electron:dir`, and packaged app smoke using `KNUTH_IM_BACKEND_COMMAND=uv`.
