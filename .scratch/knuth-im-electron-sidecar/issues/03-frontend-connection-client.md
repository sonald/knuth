# Frontend Connection Client

Status: done

## Description

Update the frontend AG-UI client helpers to use a connection object containing base URL and auth headers, using desktop metadata when available and preserving the web/dev endpoint override flow.

## Acceptance Criteria

- [x] All fetch helpers send configured auth headers.
- [x] `HttpAgent` sends the same auth headers for `/agent`.
- [x] Packaged Electron no longer depends on `NEXT_PUBLIC_KNUTH_AGUI_URL`.
- [x] The existing UI layout and visual design are not redesigned.

## Verification

- [x] `cd apps/knuth-im-web && npm run typecheck`

## Comments

- Updated `lib/agui.ts` to accept `{ baseUrl, headers }` connection metadata for all fetch helpers and `HttpAgent`.
- Updated `KnuthIMApp` to initialize from `window.knuthDesktop.backend()` without redesigning the UI.
- Verified with `npm run typecheck`.
