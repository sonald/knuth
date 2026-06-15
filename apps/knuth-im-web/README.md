# knuth-im-web

Next.js Run Timeline UI for the Knuth IM agent.

The app talks directly to `knuth-agui` with `@ag-ui/client`, renders durable run
history as a timeline, supports approval resume, and exposes the built-in
client-side `browser_context` tool. Client tool results are submitted to
`/tool_result`, then the app opens a new `/agent` resume stream for the same
thread.

## Run

Backend:

```bash
uv run knuth-im
```

Frontend:

```bash
npm install
npm run dev
```

By default the app talks to `http://127.0.0.1:8000`. Override it with
`NEXT_PUBLIC_KNUTH_AGUI_URL`.

## Desktop App

The Electron shell packages the current Next.js UI as a static desktop app.
Electron main now manages the `knuth-im` backend as a localhost sidecar. In
development it launches the workspace package with `uv run knuth-im`; packaged
builds run `npm run sidecar:build` first so Electron Builder can include the
PyInstaller `onedir` `sidecar/knuth-im/` artifact through `extraResources`.

The renderer obtains the sidecar endpoint and per-launch bearer token from the
preload bridge. The token is not persisted and is not written to repo files.
Model configuration is entered in the desktop Settings panel. Non-secret values
are stored in the app user-data directory. The API key is stored in a local
`secrets.json` file with owner-only file permissions (`0600`) and is never
returned to the renderer. Saving settings restarts the local sidecar with
`KNUTH_API_KEY`, `KNUTH_BASE_URL`,
`KNUTH_MODEL`, and `KNUTH_TIMEOUT` in the child process environment; packaged
users do not need to launch the app from a shell or manage environment
variables themselves.

First launch requires:

- Model endpoint, for example an OpenAI-compatible `/v1` URL
- Model name
- API key
- Workspace directory

Development shell:

```bash
npm run electron:dev
```

Directory package for local verification:

```bash
npm run electron:dir
```

Signed/release packaging can build on top of the same entry point:

```bash
npm run electron:build
```

The packaging scripts default `ELECTRON_MIRROR` to
`https://npmmirror.com/mirrors/electron/` so local builds do not depend on
GitHub release asset availability. Set `ELECTRON_MIRROR` before the command to
use a different mirror.

Local macOS builds are ad-hoc signed by the `afterPack` hook. Developer ID
signing/notarization is intentionally left for the release pipeline.

Sidecar smoke test:

```bash
npm run smoke:sidecar
```

Packaged sidecar binary smoke test:

```bash
npm run smoke:sidecar-binary
```

Real provider smoke test using the repository root `.env`:

```bash
npm run smoke:sidecar-real-env
```

Build only the standalone backend executable:

```bash
npm run sidecar:build
```

`sidecar:build` creates a PyInstaller `onedir` bundle instead of a `onefile`
executable. The directory form avoids unpacking the whole Python app on every
launch, which materially improves repeated desktop startup time. The build also
collects LiteLLM data files and the `tiktoken_ext` namespace plugins. Both are
required for packaged model calls: LiteLLM needs its model metadata JSON, and
`tiktoken` needs `tiktoken_ext.openai_public` for encodings such as
`cl100k_base`.
