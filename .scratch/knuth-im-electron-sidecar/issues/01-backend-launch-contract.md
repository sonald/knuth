# Backend Launch Contract And Local Auth

Status: done

## Description

Add explicit `knuth-im` host CLI flags and optional local auth so Electron can launch a backend sidecar without relying on inherited shell environment or a fixed port.

## Acceptance Criteria

- [x] CLI flags cover host, port, db path, workspace, auth token, and optional env file.
- [x] Existing `uv run knuth-im` behavior remains compatible.
- [x] Protected AG-UI endpoints require auth only when a token is configured.
- [x] Focused tests cover accepted and rejected auth paths.

## Verification

- [x] `uv run --with pytest pytest tests/test_knuth_im.py tests/test_knuth_im_sidecar.py`

## Comments

- Implemented `parse_server_config()` and optional `auth_token` support on `create_app()`.
- Verified with `uv run --with pytest pytest tests/test_knuth_im_sidecar.py tests/test_knuth_im.py tests/test_agui_spike.py` on 2026-06-14.
