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

**SystemPreamble**:
The single leading system message the runtime assembles each turn and places before the conversation. It is a computed projection of current context, not a durable fact: it is recomputed at build time from the available `SystemSection` values and never reconstructed from the event log. A run that resumes sees a freshly assembled preamble, not a snapshot of the one it started with.
_Avoid_: durable event, persisted system prompt, static config string, message from history

**SystemSection**:
An extensible fragment that contributes to the `SystemPreamble`, carrying a `source` drawn from a closed, strongly-typed set (today `base` runtime instructions and the `user`-level prompt; later skills or memory). Sections are assembled into the preamble in a defined order. New kinds of context join the preamble by introducing a new source of sections, not by reshaping the preamble. A user-injected user-level system prompt is one `source=user` section.
_Avoid_: user message, durable event, tool definition

**SystemSectionProvider**:
The additive seam that yields `SystemSection` values for a run. It only contributes fragments; it cannot alter messages or tools. This is its whole point versus a context rewriter, which has full power over the view. The runtime composes a preamble by gathering each provider's sections in injection order, so a new context source is a new provider rather than a change to the assembly.
_Avoid_: message rewriter, view transformer, middleware
