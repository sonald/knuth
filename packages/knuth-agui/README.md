# knuth-agui

AG-UI transport for an already-constructed Knuth `AgentRuntime`.

This package owns HTTP/SSE protocol adaptation only. It does not build a
runtime, choose tools, choose prompts, or set policy. Host packages such as
`knuth-im` construct the runtime and pass it to `create_app(runtime)`.

## Architecture

```
AgentRuntime
  -> RuntimeEventListener
  -> AGUITranslator
  -> ag-ui-protocol EventEncoder
  -> FastAPI StreamingResponse
```

## Endpoints

- `POST /agent`: AG-UI SSE run stream. `threadId == run_id`; missing
  `threadId` generates a canonical `run_<uuid>` id.
- `GET /threads`: list ledger runs for the conversation sidebar.
- `GET /threads/{threadId}/history`: return `MESSAGES_SNAPSHOT` rebuilt from
  the ledger.
- `GET /threads/{threadId}/messages`: alias for history.
- `GET /threads/{threadId}/approvals`: list pending approvals for restoring
  approval cards after conversation switches or reloads.
- `POST /pause`: pause a created/running run.
- `POST /approve`: resolve an approval; callers then open a new `/agent`
  resume stream.
- `POST /tool_result`: record an AG-UI client-tool result; callers then open a
  new `/agent` resume stream.

`threadId`/`runId` values must match `run_[A-Za-z0-9_-]{1,80}` before they can
enter the runtime as a durable run id.

Request-scoped AG-UI tools are exposed through an invocation overlay. They are
not registered in the runtime-wide `ToolRegistry`; external/client tools enter
the durable `waiting_tool_result` state until `/tool_result` records the
observation.

## Test

```bash
uv run python -m unittest tests.test_agui_spike
```
