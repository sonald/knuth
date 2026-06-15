# Knuth

Knuth is a local agent runtime organized around a clear boundary between model inference, runtime orchestration, tool execution, and durable run history.

## Language

**InferenceEvent**:
Low-level model stream information passed between the LLM boundary and runtime. It represents transient inference facts, including partial assistant text, reasoning, stream boundaries, tool-call deltas, complete tool calls, completion, errors, and aborts.
_Avoid_: llmd internal event, UI event, durable event

**ToolCallDelta**:
An incomplete fragment of a streamed tool call from the model boundary. It may be observed and accumulated, but it is not executable and must not be converted into a tool intent.
_Avoid_: tool call, tool intent, executable tool request

**ToolCallStarted**:
The stream boundary where the model begins constructing a tool call. It lets runtime observers and UIs represent the start of a tool-call stream without treating the call as executable.
_Avoid_: tool intent, executable tool request

**RuntimeEvent**:
Runtime-level event language for run history, orchestration, recovery, audit, hooks, and debugging. It covers the semantic projection of model stream information without being a one-to-one mirror of raw transient deltas.
_Avoid_: inference event, provider chunk, generic event

**LiveRuntimeObservation**:
Best-effort delivery of `RuntimeEvent` values to active observers while a run is executing. It is for current UI, logging, and debugging views, not for durable history, recovery, or work dispatch.
_Avoid_: message queue, durable event stream, tool execution queue

**RuntimeEventListener**:
An object registered with a live run invocation to observe selected `RuntimeEvent` values for rendering, logging, debugging, metrics, or fan-out. It declares its event interest and reacts to delivered events, but it does not return control decisions and must not be used as the path for pausing, terminating, approving, denying, or otherwise changing run state.
_Avoid_: hook, control command, work dispatcher, state transition handler, callback-only event handler

**RuntimeEventInterest**:
The observation-layer declaration of which `RuntimeEvent` values a `RuntimeEventListener` wants to receive. It may match exact dotted event types, dotted type prefixes, and durable or transient event durability, but it does not add namespace or name fields to the event model.
_Avoid_: event schema, namespace, subscription command, hook point

**BlockingHook**:
An awaited runtime extension point that can decide whether the agent loop continues, pauses, or terminates at a named point. It is a control seam, not a data mutation seam; context, tools, and policy changes belong to their explicit providers or brokers.
_Avoid_: event handler, message middleware, context provider, patch system

**HookPoint**:
A named state boundary where the runtime may await blocking hooks before continuing the agent loop. First-version hook points are limited to run-state transitions or moments immediately before external side effects, not every internal context, model, or tool observation point.
_Avoid_: event type, debug trace point, middleware stage, arbitrary callback

**RuntimeControl**:
The awaited runtime control surface for state-changing run operations such as starting, continuing, resuming, approving, denying, pausing, or cancelling a run. It owns run lifecycle transitions around the agent loop through explicit operations rather than one parameter-overloaded entrypoint; observers and CLI handlers call it rather than reconstructing lifecycle events themselves.
_Avoid_: live observation, event handler, CLI flow, message queue, overloaded run API

**RunInvocation**:
A single live attempt to advance a durable `Run`, such as starting it, continuing it with a new user message, or resuming it after approval or pause. It is represented to callers by a temporary `RunSession` and may emit transient invocation lifecycle events; it is not the durable conversation history itself.
_Avoid_: run, durable session, event store stream, daemon connection

**RunSession**:
The temporary async lifecycle handle for one `RunInvocation`. It owns the invocation task, live observation hub, listener queues, and result awaiting for that invocation; durable status and history remain available through `RuntimeControl` queries and `EventStore`.
_Avoid_: durable run model, event listener, CLI session, stored event log

**Run**:
The durable conversation history and current orchestration state for an agent interaction. A `SUCCEEDED` run means the latest agent-loop invocation completed successfully; it may be continued with a new user message, unlike `FAILED` or `CANCELLED`.
_Avoid_: permanently closed session, single model request, event stream

**RunPaused**:
A durable runtime fact that a run entered a resumable paused state because runtime control or a blocking hook interrupted progress. It explains a `PAUSED` status transition and is distinct from model abort details.
_Avoid_: model aborted, failed run, waiting for approval

**RunCancelled**:
A durable runtime fact that a run was intentionally terminated by runtime control or a blocking hook. It explains a `CANCELLED` status transition and is not a runtime failure.
_Avoid_: failed run, model error, verification failure, pause

**AgentLoop**:
The runtime-owned orchestration cycle that repeatedly builds the current context, consumes model inference, emits runtime events, handles tool intents, and continues until the run reaches a waiting or terminal status. It is a reusable runtime capability, not a CLI interaction loop or provider stream.
_Avoid_: CLI loop, REPL loop, inference stream, tool execution loop

**ToolBroker**:
The runtime-facing gateway for tool workflow. The agent loop submits tool intents to it and receives proposals or execution records; tool registry, provider selection, policy checks, and approval requirements remain behind this gateway.
_Avoid_: tool registry, policy engine, provider, direct tool call

**ToolProvider**:
The owned source of a named tool set. A provider lists the manifests it owns and executes invocations for those tools; `ToolRegistry` registers providers and indexes their manifests, but it does not mutate a provider's tool set or accept standalone tool injection. Tool names are global in one registry and conflicts must fail fast.
_Avoid_: mutable tool bucket, registry-owned tool list, override order, external tool injector

**SystemPreamble**:
The single leading system message the runtime assembles each turn and places before the conversation. It is a computed projection of current context, not a durable fact: it is recomputed at build time from the available `SystemSection` values and never reconstructed from the event log. A run that resumes sees a freshly assembled preamble, not a snapshot of the one it started with.
_Avoid_: durable event, persisted system prompt, static config string, message from history

**SystemSection**:
An extensible fragment that contributes to the `SystemPreamble`, carrying a `source` drawn from a closed, strongly-typed set (today `base` runtime instructions and the `user`-level prompt; later skills or memory). Sections are assembled into the preamble in a defined order. New kinds of context join the preamble by introducing a new source of sections, not by reshaping the preamble. A user-injected user-level system prompt is one `source=user` section.
_Avoid_: user message, durable event, tool definition

**SystemSectionProvider**:
The additive seam that yields `SystemSection` values for a run. It only contributes fragments; it cannot alter messages or tools. This is its whole point versus a context rewriter, which has full power over the view. The runtime composes a preamble by gathering each provider's sections in injection order, so a new context source is a new provider rather than a change to the assembly.
_Avoid_: message rewriter, view transformer, middleware

**RunLedger**:
The single authoritative write surface for durable run state. Every durable change is one `apply(event)` call that validates aggregate invariants, appends the event, and synchronously updates derived projections inside one transaction. Run status, approvals, and tool invocation states have no direct write path beside it; atomicity is structural, not disciplinary.
_Avoid_: event store wrapper, dual-write store, message queue, repository

**DecisionEvent**:
A durable `RuntimeEvent` designed for state reconstruction: it records an orchestration decision or fact (batch planned, invocation started, approval resolved) whose typed fold yields run state. If answering a state question requires heuristics over incidental events, a decision event type is missing and should be added instead of the heuristic.
_Avoid_: log line, debug trace, transient delta, snapshot dump

**Projection**:
A derived, rebuildable read model (runs, tool invocations, approvals, conversation) computed by folding decision events. It is updated synchronously in the same transaction as the event append and can always be dropped and refolded; changing a projection's schema is not a data migration. Event type shapes are the only durable contract.
_Avoid_: authoritative state, source of truth, cache that can silently drift

**ToolInvocation**:
The per-tool-call state machine projection, keyed by `tool_call_id` and carrying `args_hash`, `effect`, and `risk`. Its states are proposed, awaiting_approval, approved, waiting_tool_result, denied, running, succeeded, failed, and unknown. It is the unit the agent loop schedules and the unit crash recovery reasons about; it subsumes what other designs call a pending action or execution record.
_Avoid_: tool intent, pending action, execution log entry, queue item

**WaitingToolResult**:
The resumable invocation state for an external/client-executed tool that has been approved by policy but is intentionally waiting for an out-of-process result to be submitted. It is normal control flow and resumes by appending a tool completion event, unlike `UnknownOutcome`, which is crash recovery for an indeterminate side effect.
_Avoid_: unknown outcome, failed tool, pending approval, background execution

**ToolBatch**:
The set of tool calls produced by one assistant turn, opened by `tool.batch_planned` and closed by `tool.batch_closed` only when every invocation in it has a model-visible observation. At most one batch is open per run; an open batch is the run's resume point after approval, pause, or crash.
_Avoid_: parallel task queue, message group, tool cache

**ContextSnapshot**:
The frozen, hash-level proof of what one model call saw: message, tool, preamble, and model-config hashes plus counts, recorded on `step.started`. It answers "why did the model do that" by proving whether two builds saw the same input, without persisting the full prompt.
_Avoid_: full prompt dump, persisted system prompt, message history

**UnknownOutcome**:
The terminal-pending invocation state for an external-write tool that was started but whose completion was never recorded, typically a crash mid-flight. The side effect may or may not have happened, so it must be resolved by a human decision and never auto-retried; resolution appends the human-confirmed completion event.
_Avoid_: failed, retryable error, timeout, denial

**AGUITransport**:
The boundary that adapts an already-constructed `AgentRuntime` to a Web (AG-UI / CopilotKit) frontend, as knuth-cli adapts one to a terminal. It never builds a runtime and never decides server-side tools, prompt, or policy — those are agent policy and belong to the knuth-im host/runtime factory. It may own the AG-UI client-tool provider type because that is protocol adaptation, but the host explicitly registers that provider in the runtime and passes the same provider to `create_app(runtime, client_tool_provider=...)`. It observes runtime events and translates them to the AG-UI event vocabulary, routes control intent to `RuntimeControl` (`start`/`continue_run`/`resume`/`approve`/`pause`/`submit_tool_result`), and answers read-only ledger queries. It owns no agent policy, holds no run state of its own, and leaks no HTTP/AG-UI concept back into the runtime; the runtime may grow only additive, audience-neutral seams to serve it. Its concrete adapters and protocol mapping live in the knuth-agui package and ADR-006, not in this glossary.
_Avoid_: runtime extension, agent policy owner, runtime factory, control authority, server tool provider owner, CopilotKit/HTTP concept inside runtime, Node CopilotRuntime
