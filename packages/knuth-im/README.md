# knuth-im

Host/runtime wiring for the Knuth IM web agent.

`knuth-im` owns environment loading, model configuration, SQLite ledger choice,
CLI system prompt/tool wiring, and the process entrypoint. It passes the
constructed `AgentRuntime` into `knuth-agui.create_app(runtime)`.

The host currently exposes the same local tool provider as `knuth-cli`
(`read_file`, `write_file`, `edit_file`, `glob`, `grep`, `shell`, `python`).
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
