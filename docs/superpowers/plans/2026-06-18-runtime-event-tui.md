# Runtime Event TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the runtime event terminal workbench described in `docs/2026-06-18-runtime-event-tui-design.md`.

**Architecture:** Add one transient runtime event for the synthesized system preamble, then build a reusable Textual TUI under `knuth_cli.runtime_event_tui` with a thin script entry point. The TUI observes via `RuntimeEventListener`, reads durable projections through `AgentRuntime`, and performs only minimal control actions through public runtime APIs.

**Tech Stack:** Python 3.12, AnyIO, Pydantic typed runtime events, Textual, Rich, `uv run python -m unittest`.

---

### Task 1: Transient System Preamble Event

**Files:**
- Modify: `packages/knuth-core/src/knuth/core/runtime_events.py`
- Modify: `packages/knuth-core/src/knuth/core/events.py`
- Modify: `packages/knuth-runtime/src/knuth_runtime/loop.py`
- Test: `tests/test_core.py`
- Test: `tests/test_runtime.py`

- [x] **Step 1: Add failing core event test**

Add a test that constructs `ContextSystemPreambleBuiltDraft(content="SYSTEM")`, emits it with `emit_transient_runtime_event`, and asserts its type, durability, run id, and content.

- [x] **Step 2: Add failing runtime receive-order tests**

Add tests proving a live listener receives `context.system_preamble.built` before `step.started`, that its content matches the leading system prompt, that no-preamble runs emit `content=None`, and that `runtime.events(run_id)` does not include the transient event.

- [x] **Step 3: Implement event types and exports**

Add `ContextSystemPreambleBuiltDraft` and `ContextSystemPreambleBuilt`, include them in `TransientRuntimeEventDraft`, `TransientRuntimeEvent`, `RuntimeEvent`, and `knuth.core.events` exports.

- [x] **Step 4: Emit from `_run_step`**

After context build and before `StepStartedDraft`, inspect `view.messages[0]`; if it is a system message, emit the text, otherwise emit `None`.

- [x] **Step 5: Run focused runtime tests**

Run:

```sh
uv run python -m unittest tests/test_core.py tests/test_runtime.py -v
```

Expected: the new tests pass, with no regressions in existing core/runtime tests.

### Task 2: Runtime Event TUI Models, Views, And Capture

**Files:**
- Create: `packages/knuth-cli/src/knuth_cli/runtime_event_tui/__init__.py`
- Create: `packages/knuth-cli/src/knuth_cli/runtime_event_tui/models.py`
- Create: `packages/knuth-cli/src/knuth_cli/runtime_event_tui/views.py`
- Create: `packages/knuth-cli/src/knuth_cli/runtime_event_tui/capture.py`
- Test: `tests/test_runtime_event_tui.py`

- [x] **Step 1: Add failing model/view/capture tests**

Add tests for event row labels, JSON detail rendering, `context.*` and durability filters, latest system preamble extraction, and capture listener receive order.

- [x] **Step 2: Implement row and snapshot models**

Create frozen dataclasses for `ObservedEventRow`, `RunSnapshot`, and `ApprovalRow` with only fields used by the TUI.

- [x] **Step 3: Implement view helpers**

Implement label formatting, JSON rendering, event filtering, durable/live de-duplication, and latest system preamble lookup.

- [x] **Step 4: Implement capture listener**

Implement `RuntimeEventCapture` with `RuntimeEventInterest.all()`, blocking overflow, monotonically increasing receive indices, and listener stats exposure.

- [x] **Step 5: Run focused TUI helper tests**

Run:

```sh
uv run python -m unittest tests/test_runtime_event_tui.py -v
```

Expected: helper and capture tests pass.

### Task 3: Runtime Event TUI Controller

**Files:**
- Create: `packages/knuth-cli/src/knuth_cli/runtime_event_tui/controller.py`
- Test: `tests/test_runtime_event_tui.py`

- [x] **Step 1: Add failing controller tests**

Add a fake runtime and assert the controller calls only `start`, `resume`, `events`, `messages`, `model_context_messages`, `rewrite_audit`, `pending_approvals`, `approve`, and `deny`.

- [x] **Step 2: Implement controller**

Implement methods to start a prompt, load history, resume, approve, deny, and refresh the current run snapshot. Keep errors as status text instead of mutating optimistic state.

- [x] **Step 3: Run controller tests**

Run:

```sh
uv run python -m unittest tests/test_runtime_event_tui.py -v
```

Expected: controller tests pass.

### Task 4: Textual App And Script Entry Point

**Files:**
- Create: `packages/knuth-cli/src/knuth_cli/runtime_event_tui/app.py`
- Create: `scripts/runtime_event_tui.py`
- Test: `tests/test_runtime_event_tui.py`

- [x] **Step 1: Add failing app mount test**

Use Textual `run_test` to assert the prompt input, event list, detail pane, inspector, and filter controls mount.

- [x] **Step 2: Implement Textual app**

Build the one-screen workbench with run bar, event timeline, event detail pane, inspector, action bindings, and update methods that call the controller.

- [x] **Step 3: Implement script entry point**

Create a thin script that builds the normal CLI runtime and launches `RuntimeEventTui`.

- [x] **Step 4: Run app tests**

Run:

```sh
uv run python -m unittest tests/test_runtime_event_tui.py -v
```

Expected: the app mounts and helper/controller tests still pass.

### Task 5: Verification And Completion Audit

**Files:**
- Inspect: `docs/2026-06-18-runtime-event-tui-design.md`
- Inspect: changed source and tests

- [x] **Step 1: Run required verification commands**

Run:

```sh
uv run python -m unittest tests/test_core.py tests/test_runtime.py tests/test_runtime_event_tui.py -v
uv run python -m compileall packages tests scripts
git diff --check
```

Expected: all commands pass.

- [x] **Step 2: Audit spec requirements**

Compare each in-scope and out-of-scope item in the design doc against current files and test evidence. Confirm the implementation adds no durable event, ledger schema change, active stop/pause/cancel control, crash recovery UI, unknown outcome resolver, or low-level inference event browser.

- [x] **Step 3: Report status**

Summarize changed files, verification results, and any residual risks.
