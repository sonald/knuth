# E2E And Packaging Verification

Status: done

## Description

Add and run focused verification that the sidecar contract works end to end and that Electron packaging still succeeds after the sidecar integration.

## Acceptance Criteria

- [x] Python focused tests pass.
- [x] Frontend typecheck passes.
- [x] Electron directory package builds.
- [x] Smoke test starts a backend sidecar and confirms health plus auth behavior.
- [x] Generated Electron outputs remain ignored.

## Verification

- [x] `uv run --with pytest pytest tests/test_agui_spike.py tests/test_knuth_im.py tests/test_knuth_im_sidecar.py`
- [x] `cd apps/knuth-im-web && npm run typecheck`
- [x] `cd apps/knuth-im-web && npm run electron:dir`
- [x] `cd apps/knuth-im-web && node scripts/smoke-sidecar.mjs`
- [x] `cd apps/knuth-im-web && npm run smoke:sidecar-binary`
- [x] `cd apps/knuth-im-web && npm run smoke:sidecar-real-env`

## Comments

- Focused Python tests, frontend typecheck, sidecar smoke, Electron directory packaging, codesign verification, app.asar inspection, sidecar resource inspection, standalone sidecar smoke, and packaged app smoke all passed.
- Packaged smoke verified `Contents/Resources/knuth-im-sidecar/knuth-im` launches directly without `KNUTH_IM_BACKEND_COMMAND`; token is supplied through child env, not `--auth-token` argv.
- Fixed packaged model-call failures by collecting LiteLLM data files in PyInstaller (`--collect-data litellm`).
- Fixed real packaged model-call failures by collecting `tiktoken_ext` namespace submodules; without `tiktoken_ext.openai_public`, the binary failed with `Unknown encoding cl100k_base`.
- Increased backend health polling to 120 seconds so onefile sidecar cold start does not get misreported as `backend unavailable`.
- Switched packaged sidecar output from PyInstaller `onefile` to `onedir`. Measured onefile startup to `/healthz` at 23.85s, 13.52s, and 14.51s; measured onedir startup at 23.07s cold and 0.40s / 0.39s warm. The rebuilt Electron app reached sidecar health in 3.41s during verification.
- Added `npm run smoke:sidecar-binary` to exercise the packaged binary through `/agent` and catch missing bundled LiteLLM/tiktoken metadata.
- Added `npm run smoke:sidecar-real-env` to exercise the packaged binary against the repository root `.env` and a real model provider without printing secrets.
