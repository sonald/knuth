# Runtime Event TUI Design

Date: 2026-06-18

## Goal

Build a terminal debug workbench for Knuth runtime events.

The first version is live-run first: the user enters a prompt, the tool starts a
runtime invocation, captures live `RuntimeEvent` values, and then reloads the
same run's durable history and projections after the invocation settles. It also
supports loading an existing run by `run_id`.

This tool is for runtime-level debugging. It observes `RuntimeEvent` values and
durable runtime projections. It is distinct from `scripts/llmd_event_tui.py`,
which inspects low-level `InferenceEvent` streams at the model boundary.

## Scope

In scope:

- Start a prompt as a live runtime run.
- Load durable history for an existing run.
- Show live transient and durable runtime events in receive order.
- Show selected event details as JSON.
- Show run status, pending approvals, raw ledger messages, model context
  messages, rewrite audit, listener stats, and the latest system preamble.
- Filter events by durability, common type prefixes, or a custom prefix.
- Support minimal control actions: approve, deny, and resume.
- Add one transient runtime event for viewing the final synthesized system
  prompt used by a model request.

Out of scope:

- Stop, pause, or cancel active runtime work.
- Recover crashed runs.
- Resolve unknown tool outcomes.
- Inspect low-level `InferenceEvent` streams.
- Persist full model input bodies.
- Write system prompt text into the ledger or conversation history.
- Change SQLite schema or durable event contracts.

## Entry Points And Modules

The script entry point will be:

```text
scripts/runtime_event_tui.py
```

The reusable implementation will live under:

```text
packages/knuth-cli/src/knuth_cli/runtime_event_tui/
```

Planned modules:

- `app.py`: Textual widgets, layout, key bindings, and user interactions.
- `controller.py`: Runtime orchestration for start, load history, approve,
  deny, and resume.
- `capture.py`: A `RuntimeEventListener` that captures live events in receive
  order.
- `views.py`: Formatting helpers for event labels, event details, projections,
  approvals, and status text.
- `models.py`: Small TUI view models, such as event rows, run snapshots, and
  approval rows.

The script stays thin: parse arguments, build the standard CLI runtime, and
launch the Textual app.

## Runtime Event Addition

Add exactly one transient runtime event:

```python
class ContextSystemPreambleBuiltDraft(TransientRuntimeEventDraftBase):
    type: Literal["context.system_preamble.built"] = (
        "context.system_preamble.built"
    )
    content: str | None
```

The event answers one debug question that cannot be answered from durable events:
what full leading system prompt did this model request actually see?

No additional body fields are needed:

- `run_id` already exists on the runtime event envelope.
- The preamble hash, message count, and tool count are already available from
  the adjacent `step.started.snapshot`.
- The event type itself means the content is the leading system message for the
  imminent model request.

The event is transient only. It must not enter the durable event union, the
ledger, SQLite, or conversation history.

## Emit Point

The event is emitted in `_run_step` after final context construction and before
the model request:

1. Run message middleware checkpoint for `BEFORE_MODEL_REQUEST`.
2. Build the final `ContextView`.
3. Read the leading system message from `view.messages[0]` when present.
4. Emit `context.system_preamble.built` with `content` set to that text, or
   `None` when there is no leading system message.
5. Emit `step.started`.
6. Call `inference_client.stream(...)`.

This gives the TUI the complete prompt text while preserving the durable model
input proof as `step.started.snapshot`.

## TUI Layout

The app uses one workbench screen.

Top run bar:

- Prompt input.
- Run and resume controls.
- Current or requested `run_id`.
- Current status.
- Event filter.

Main body:

- Event timeline: receive-order rows for live events and loaded durable events.
- Event detail: complete JSON for the selected event.
- Run inspector: status, pending approvals, raw ledger messages, model context
  messages, rewrite audit, listener stats, and latest system preamble.

Action bar:

- `Ctrl+R`: run current prompt.
- `Ctrl+L`: load history for the current `run_id`.
- `Ctrl+F`: focus filter.
- `A`: approve the selected or first pending approval.
- `D`: deny the selected or first pending approval.
- `R`: resume current run.
- `Esc`: cancel only local TUI interaction where safe; it does not stop runtime
  work.
- `Ctrl+Q`: quit.

## Event Rows

The TUI keeps a small row model:

```python
ObservedEventRow(
    source="live" | "durable",
    receive_index=int | None,
    durable_seq=int | None,
    event_type=str,
    durability="transient" | "durable",
    run_id=str,
    event=RuntimeEvent,
)
```

Rows loaded from durable history are de-duplicated against live durable events
by event id or `(run_id, seq)`. Transient rows are never de-duplicated against
durable history because they do not exist in the ledger.

The main timeline preserves receive order for live debugging. The run inspector
has a separate System Preamble section that shows the latest
`context.system_preamble.built.content` so the user can inspect the leading model
input before the first model-visible user message.

## Control Boundaries

The TUI observes through `RuntimeEventListener` and reads durable state through
public `AgentRuntime` APIs.

Allowed state-changing calls:

- `runtime.approve(approval_id)`
- `runtime.deny(approval_id)`
- `runtime.resume(run_id, listeners=[capture])`
- `runtime.start(prompt, listeners=[capture])`

The TUI must not write the ledger directly, synthesize durable events, or treat a
listener as a control hook.

## Error Handling

- Runtime start or resume failure keeps already captured rows visible and shows
  the error in the status/detail area.
- Approval errors do not optimistically change local approval state; the user can
  reload approvals or history.
- The capture listener uses blocking overflow behavior in the first version so a
  debug session does not silently drop events.
- Listener stats are shown in the inspector.
- Empty preamble is represented by `content=None`, making "no system prompt" a
  visible observed fact rather than an ambiguous missing event.

## Tests

Runtime tests:

- `context.system_preamble.built` is a transient runtime event and serializes
  through the existing typed event machinery.
- A live listener receives the event before `step.started`.
- The event content matches the final leading system message for the request.
- When no system prompt exists, the event is emitted with `content=None`.
- `runtime.events(run_id)` does not include the event.

TUI tests:

- Event labels show source, receive order, durability, and type.
- Detail rendering shows the full event JSON.
- Filters match `context.*`, durability filters, and custom prefixes.
- The inspector shows the latest system preamble.
- Controller approve, deny, and resume paths call only public `AgentRuntime`
  methods.
- The Textual app mounts under `run_test` with the main panes present.

Verification commands:

```sh
uv run python -m unittest tests/test_core.py tests/test_runtime.py tests/test_runtime_event_tui.py -v
uv run python -m compileall packages tests scripts
git diff --check
```
