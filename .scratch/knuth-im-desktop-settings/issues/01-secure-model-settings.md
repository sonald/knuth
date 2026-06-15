# Secure Model Settings

Status: done

## Description

Implement app-owned model configuration for the Electron desktop shell, with local API key storage and sidecar restart after save.

## Acceptance Criteria

- [x] Main process persists public settings under app user data.
- [x] Main process stores API key in a local user-data secret file with mode `0600`.
- [x] Preload exposes only public settings and save/restart methods.
- [x] Backend manager reads saved settings before launching the sidecar.
- [x] Renderer settings form does not display the raw saved key.
- [x] Missing settings open the settings panel and block new runs.
- [x] Verification commands pass.

## Comments

- Added `electron/settings-store.cjs` with boundary validation and app-owned secret storage.
- Switched away from Electron `safeStorage` after local builds repeatedly prompted for keychain access and made saved keys hard to reuse.
- Added IPC handlers for settings load/save, workspace picker, and backend restart.
- Updated the sidebar settings panel to edit model and workspace settings.
- Decoupled settings loading from backend startup in the renderer so saved model configuration appears immediately while the sidecar is still starting.
- Added an explicit `Starting` connection state and disabled message submission until the desktop sidecar is ready.
- Verified with `npm run smoke:settings`, `npm run typecheck`, `npm run build`, `npm run smoke:sidecar`, `npm run electron:dir`, and the focused `uv run --with pytest pytest ...` suite.
