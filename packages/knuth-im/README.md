# knuth-im

Host/runtime wiring for the Knuth IM web agent.

`knuth-im` owns environment loading, model configuration, SQLite ledger choice,
CLI system prompt/tool wiring, and the process entrypoint. It passes the
constructed `AgentRuntime` into `knuth-agui.create_app(runtime)`.

The host currently exposes the same local tool surface as `knuth-cli`: default
server tools (`read_file`, `write_file`, `shell`, `python`) plus CLI-local
tools (`edit_file`, `glob`, `grep`).
Risky tools still go through the runtime policy/approval path.

## Run

Set `KNUTH_API_KEY`, `KNUTH_BASE_URL`, and `KNUTH_MODEL` in the environment or
repo-root `.env`, then run:

```bash
uv run knuth-im
```

Optional environment:

- `KNUTH_TIMEOUT`, default `60`
- `KNUTH_IM_HOST`, default `127.0.0.1`
- `KNUTH_IM_PORT`, default `8000`
- `KNUTH_IM_DB_PATH`, default `~/.knuth/knuth-im.db`
- `KNUTH_IM_WORKSPACE`, optional process working directory for local tools
- `KNUTH_IM_AUTH_TOKEN`, optional local bearer token for protected endpoints
- `KNUTH_IM_ENV_FILE`, default `.env`

Equivalent CLI flags are available for sidecar hosts:

```bash
uv run knuth-im \
  --host 127.0.0.1 \
  --port 8000 \
  --db-path ~/.knuth/knuth-im.db \
  --workspace /path/to/workspace \
  --auth-token "$KNUTH_IM_AUTH_TOKEN"
```

When `--auth-token` is set, `/healthz` remains public for startup polling and
other AG-UI endpoints require `Authorization: Bearer <token>` or
`X-Knuth-Auth-Token`.
