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
