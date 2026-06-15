# PRD: Knuth IM Desktop Settings

## Status

Done

## Goal

Let packaged Knuth IM users configure model provider settings inside the app instead of launching the app with user-managed environment variables.

## Scope

- Store non-secret desktop configuration in the Electron user-data directory.
- Store the model API key in a local app user-data secret file with owner-only permissions.
- Expose a narrow preload bridge for public settings, save, workspace picking, and backend restart.
- Start the Electron-managed sidecar from saved settings and report `needs_settings` when required model fields are missing.
- Add a compact settings UI to the existing sidebar without redesigning the chat surface.
- Keep development/web endpoint override behavior available.

## Non-Goals

- Moving model configuration ownership into `knuth-runtime` or `knuth-agui`.
- Storing API keys in app config, repo files, or renderer-visible state.
- Adding multi-profile provider management.
- Adding release notarization or installer onboarding.

## Acceptance Criteria

- [x] Renderer never receives the raw API key; it only receives `hasApiKey` and source metadata.
- [x] Saving an API key writes only to a local secret file and keeps it out of renderer-visible state.
- [x] Missing model config returns a backend `needs_settings` state instead of starting a broken sidecar.
- [x] Saving settings restarts the sidecar with model config passed to the child process.
- [x] The desktop UI supports model endpoint, model, API key, timeout, workspace, and database path.
- [x] Existing web/dev AG-UI endpoint override remains available outside desktop mode.
- [x] Verification commands pass.

## Verification Commands

- `cd apps/knuth-im-web && npm run smoke:settings`
- `cd apps/knuth-im-web && npm run typecheck`
- `cd apps/knuth-im-web && npm run build`
- `cd apps/knuth-im-web && npm run smoke:sidecar`
- `cd apps/knuth-im-web && npm run electron:dir`
- `uv run --with pytest pytest tests/test_knuth_im_sidecar.py tests/test_knuth_im.py tests/test_agui_spike.py`
