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
