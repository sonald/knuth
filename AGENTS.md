# AGENTS

- 这是一个 uv 工程，所有命令都用 `uv` 运行。

## Agent skills

### Issue tracker

Issues and PRDs are tracked as local markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Triage uses the default five-role label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo: read root `CONTEXT.md` when present and architectural decisions under `docs/decisions/`. See `docs/agents/domain.md`.

## IM desktop app build

The Electron/Next.js app lives in `apps/knuth-im-web`.

Recommended local directory build:

```sh
cd apps/knuth-im-web
npm run sidecar:build
npm run typecheck
npm run build
ELECTRON_MIRROR=${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/} npx electron-builder --mac --dir --publish never
npm run smoke:sidecar-binary
```

Output app:

```text
apps/knuth-im-web/dist-electron/mac-arm64/Knuth IM.app
```

If `npm run typecheck` fails from stale Next generated types under `.next` (for example references to removed routes), remove `.next` and rerun `npm run typecheck` and `npm run build`.
