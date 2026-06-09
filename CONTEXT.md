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

**RuntimeEventHandler**:
An observer that reacts to delivered `RuntimeEvent` values for rendering, logging, debugging, or fan-out. It does not return control decisions and must not be used as the path for pausing, terminating, approving, denying, or otherwise changing run state.
_Avoid_: hook, control command, work dispatcher, state transition handler

**BlockingHook**:
An awaited runtime extension point that can decide whether the agent loop continues, pauses, or terminates at a named point. It is a control seam, not a data mutation seam; context, tools, and policy changes belong to their explicit providers or brokers.
_Avoid_: event handler, message middleware, context provider, patch system

**HookPoint**:
A named state boundary where the runtime may await blocking hooks before continuing the agent loop. First-version hook points are limited to run-state transitions or moments immediately before external side effects, not every internal context, model, or tool observation point.
_Avoid_: event type, debug trace point, middleware stage, arbitrary callback

**RuntimeControl**:
The awaited runtime control surface for state-changing run operations such as starting, continuing, resuming, approving, denying, pausing, or cancelling a run. It owns run lifecycle transitions around the agent loop through explicit operations rather than one parameter-overloaded entrypoint; observers and CLI handlers call it rather than reconstructing lifecycle events themselves.
_Avoid_: live observation, event handler, CLI flow, message queue, overloaded run API

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

**SystemPreamble**:
The single leading system message the runtime assembles each turn and places before the conversation. It is a computed projection of current context, not a durable fact: it is recomputed at build time from the available `SystemSection` values and never reconstructed from the event log. A run that resumes sees a freshly assembled preamble, not a snapshot of the one it started with.
_Avoid_: durable event, persisted system prompt, static config string, message from history

**SystemSection**:
An extensible fragment that contributes to the `SystemPreamble`, carrying a `source` drawn from a closed, strongly-typed set (today `base` runtime instructions and the `user`-level prompt; later skills or memory). Sections are assembled into the preamble in a defined order. New kinds of context join the preamble by introducing a new source of sections, not by reshaping the preamble. A user-injected user-level system prompt is one `source=user` section.
_Avoid_: user message, durable event, tool definition

**SystemSectionProvider**:
The additive seam that yields `SystemSection` values for a run. It only contributes fragments; it cannot alter messages or tools. This is its whole point versus a context rewriter, which has full power over the view. The runtime composes a preamble by gathering each provider's sections in injection order, so a new context source is a new provider rather than a change to the assembly.
_Avoid_: message rewriter, view transformer, middleware
