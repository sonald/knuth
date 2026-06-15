# PRD: Knuth IM Electron Sidecar

## Status

Done

## Goal

Turn the current Electron-packaged `knuth-im-web` frontend into a desktop app that can launch and talk to a local `knuth-im` backend managed by Electron main. The runtime remains Knuth-owned; Electron owns process lifecycle and renderer connection metadata.

## Scope

- Add an explicit `knuth-im` backend launch contract for host, port, database path, workspace cwd, auth token, and optional env file.
- Add local-only HTTP auth for sidecar-managed backend endpoints without leaking auth concepts into runtime.
- Add an Electron backend manager that starts the sidecar, polls health, exposes connection metadata through preload, and terminates the sidecar on app exit.
- Update the existing frontend API client to use desktop-provided endpoint metadata and headers while preserving manual endpoint override for web/dev use.
- Add focused tests and an end-to-end smoke that proves the backend can be launched and queried through the sidecar path.

## Non-Goals

- Redesigning or replacing the user's current frontend UI.
- Moving AG-UI, HTTP, or Electron concepts into `knuth-runtime`.
- Bundling Python source inside `app.asar`.
- Requiring secrets to be stored in the app bundle, repository, or logs.
- Solving Developer ID signing or notarization.
- Adding multi-user auth or a background SessionManager.

## Acceptance Criteria

- [x] `uv run knuth-im` remains compatible with existing env-based usage.
- [x] `uv run knuth-im --host ... --port ... --db-path ... --workspace ... --auth-token ...` starts a backend with the requested contract.
- [x] Protected AG-UI endpoints reject missing/wrong auth when an auth token is configured.
- [x] `/healthz` remains usable for startup polling and does not expose secrets.
- [x] Electron main can launch a backend process, wait for health, expose `{ baseUrl, headers }` to preload/renderer, and cleanly stop the child process.
- [x] The frontend API helper sends auth headers for all backend calls, including `HttpAgent`.
- [x] Electron packaging still builds without putting generated outputs or `node_modules` in `app.asar`.
- [x] End-to-end smoke proves a locally launched sidecar responds to `/healthz` and rejects/allows authenticated requests as expected.
- [x] A packaged build contains a standalone `knuth-im` backend executable rather than only the placeholder resource.

## Verification Commands

- `uv run --with pytest pytest tests/test_agui_spike.py tests/test_knuth_im.py`
- `uv run --with pytest pytest tests/test_knuth_im_sidecar.py`
- `cd apps/knuth-im-web && npm run typecheck`
- `cd apps/knuth-im-web && npm run electron:dir`
- `cd apps/knuth-im-web && node scripts/smoke-sidecar.mjs`
- `cd apps/knuth-im-web && npm run smoke:sidecar-binary`
- `cd apps/knuth-im-web && npm run smoke:sidecar-real-env`
- Packaged app smoke without `KNUTH_IM_BACKEND_COMMAND` verifies the app launches `Contents/Resources/knuth-im-sidecar/knuth-im` directly.
