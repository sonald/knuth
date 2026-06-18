# Knuth

Knuth is a local agent runtime organized around a clear boundary between model inference, runtime orchestration, tool execution, and durable run history.

## Language

**InferenceEvent**:
Low-level model stream information passed between the LLM boundary and runtime. It represents transient inference facts, including partial assistant text, reasoning, stream boundaries, tool-call deltas, complete tool calls, completion, errors, and aborts.
_Avoid_: llmd internal event, UI event, durable event

**InferenceAborted**:
The explicit inference outcome emitted when a model request observes an `InterruptSignal` and cooperatively aborts the current request. The runtime handles it at an `InterruptSafePoint` as interrupted active work, not as provider failure and not as a resumable pause. The old request is never replayed; a later invocation starts from durable context. A `ModelVisibleNotice` is optional and written only if the next model call must know that the previous assistant generation was interrupted.
_Avoid_: network failure, provider error, paused run, replayable request

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
The awaited runtime control surface for state-changing run operations such as starting, continuing, resuming, approving, denying, pausing, or cancelling a run. It owns run lifecycle transitions around the agent loop through explicit operations rather than one parameter-overloaded entrypoint; observers and CLI handlers call it rather than reconstructing lifecycle events themselves. `resume` means continue an existing unfinished control point such as `PAUSED`, `WAITING_APPROVAL`, or `WAITING_TOOL_RESULT`; `continue_run` means add new user input and start a new invocation. An `INTERRUPTED` run is not resumable because the active work was abandoned and must not replay the old model request or tool batch.
_Avoid_: live observation, event handler, CLI flow, message queue, overloaded run API, active session manager

**RunInvocation**:
A single live attempt to advance a durable `Run`, such as starting it, continuing it with a new user message, or resuming it after approval or pause. It is represented to callers by a temporary `RunSession` and may emit transient invocation lifecycle events; it is not the durable conversation history itself.
_Avoid_: run, durable session, event store stream, daemon connection

**RunSession**:
The temporary async lifecycle handle for one `RunInvocation`. It owns the invocation task, live observation hub, listener queues, result awaiting, and live interrupt handling for that invocation; durable status and history remain available through `RuntimeControl` queries and `EventStore`.
_Avoid_: durable run model, event listener, CLI session, stored event log, runtime session registry

**InteractiveReentry**:
The behavior of an interactive driver when it starts or returns after a local exit such as Ctrl+C. It first restores the latest actionable run state instead of showing an ordinary blank prompt: `WAITING_APPROVAL` re-shows approval, `WAITING_TOOL_RESULT` restores the wait, `PAUSED` offers resume, and a live `RUNNING` invocation is attached for observation when available. A `RUNNING` run with no live session in the current process is not auto-recovered until Knuth has a live-run lease/heartbeat; the driver must require explicit recovery or confirmation because another process may still be executing it. `INTERRUPTED` and `SUCCEEDED` return to ordinary prompt mode where the next user-authored input becomes `continue_run`. If more than one actionable run exists, the driver must ask the user to choose or require an explicit run id.
_Avoid_: runtime control concept, model input parser, automatic resume of abandoned work, hidden approval

**Run**:
The durable conversation history and current orchestration state for an agent interaction. A `SUCCEEDED` run means the latest agent-loop invocation completed successfully; it may be continued with a new user message, unlike `FAILED` or `CANCELLED`.
_Avoid_: permanently closed session, single model request, event stream

**Interrupt**:
A request to stop in-flight agent work, normalized from interactive input, UI controls, transport signals, daemon commands, timeouts, or blocking hooks. In Knuth an interrupt is always a cancellation, never a silent pause: it discards current work rather than recording a resume-the-same-work point.
_Avoid_: pause, suspend-and-resume, abort-as-pause, cancel token

**InterruptSignal**:
The live, normalized control signal that carries an `Interrupt` through the active runtime, model, tool, and UI layers before it becomes a durable run fact. Each layer observes it at its own safe points; execution cancellation is only the backing mechanism, and the signal itself is not durable history. The controller provides sticky observation plus a wakeup path for blocking awaits; safe points must still distinguish explicit `signal.interrupted` from ordinary teardown cancellation before writing durable facts. Tool execution receives the signal cooperatively, so the tool/provider can decide at its own safe points whether it stopped cleanly, completed before the interrupt, failed, or cannot determine the outcome.
_Avoid_: OS signal, cancellation primitive, event log entry, pause request

**ForceStop**:
A driver or supervisor escape hatch used after a graceful `InterruptSignal` has not completed quickly enough, such as a second Ctrl+C. It may tear down a CLI interaction, disconnect a UI, or kill a child process, but it does not create a separate run status and must not forge a clean interrupted tool outcome. If durable state was not safely written before force stop, recovery handles the remaining run or tool state conservatively.
_Avoid_: second interrupt kind, durable outcome, pause, successful cancellation

**InterruptSafePoint**:
A runtime-defined boundary where an in-flight interrupt may be converted into durable run and tool facts. Async cancellation may arrive at lower-level awaits, but only an interrupt safe point may explain the result in the ledger. When backing cancellation is used to wake a blocking await, the safe point must first verify `signal.interrupted`, then shield durable ledger writes so cancellation does not swallow the facts that close the interrupted work. Multi-event tool-batch interrupt collapse must use `RunLedger.apply_many(...)` or an equivalent single transaction. Interrupted attempts may remain audit records, but they must not consume the user-visible `max_turns` budget; `run.steps` remains a monotonic attempt counter, while max-turn enforcement uses a separate completed/committed turn count.
_Avoid_: arbitrary await, signal handler, cleanup callback, exception site

**RunInterrupted**:
A durable runtime fact that the current active `RunInvocation` was cancelled by a turn-scoped interrupt while the `Run` stays alive. It explains an `INTERRUPTED` status transition: in-flight work is abandoned rather than resumed, and the run may be continued by later user input or runtime-controlled follow-up. This covers Ctrl+C and UI stop controls while model or tool work is active; it does not apply to interrupting a local approval prompt, which leaves the run in `WAITING_APPROVAL` and should re-show the same confirmation on re-entry. If the interrupt happens inside an open tool batch, unstarted invocations must first receive abandoned/interrupted observations so a later resume cannot run them; if the active invocation outcome is known, observations, batch closure, a brief user-stop notice, and `RunInterrupted` are committed atomically; if it is unknown, the run pauses for recovery with remaining invocations already abandoned. Distinct from `RunPaused` (resume the same unfinished work) and `RunCancelled` (terminate the whole run).
_Avoid_: pause, resume point, run cancellation, kill thread, succeeded

**RunPaused**:
A durable runtime fact that a run entered a resumable paused state where the same in-flight work is meant to be resumed. It is driven by the runtime itself — crash recovery, an unresolved `UnknownOutcome`, a model abort, or a blocking hook — not by a user stop control. It explains a `PAUSED` status transition.
_Avoid_: Ctrl+C interrupt, UI stop, failed run, waiting for approval

**RunCancelled**:
A durable runtime fact that a whole `Run` was intentionally terminated, the run-scoped `Interrupt` triggered by an explicit `/cancel` (or runtime control / blocking hook). It explains a terminal `CANCELLED` transition: the run is neither resumable nor continuable, and it is not a runtime failure. Distinct from `CancelTurn`, which keeps the run alive.
_Avoid_: failed run, model error, verification failure, pause, cancel turn

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

**ModelVisibleNotice**:
A synthetic runtime/conversation fact inserted by Knuth only when the next model call must be told about a runtime orchestration outcome, such as active work being abandoned after an interrupt. It is not authored by the human user and must not be stored as a normal user message, but when building inference input it is projected as an `InferenceMessage` with `role=User` so every provider sees it in the ordinary conversation channel. Because it becomes a user-role inference message, it may only be inserted at a provider-valid conversation boundary: if the previous assistant turn has tool calls, matching tool observations and batch closure must exist first. For `user_stop` during active work, it should briefly express that the previous turn was stopped by the user and the model should not default-retry old actions; tool observations still carry per-tool mechanics. Waiting states such as `WAITING_APPROVAL` may handle an interactive Ctrl+C by leaving the run at the same approval prompt and re-showing that prompt on re-entry, with no notice injected.
_Avoid_: user-authored message, system preamble, hidden runtime state, tool result

**RunLedger**:
The single authoritative write surface for durable run state. Every durable change is validated, appended, and projected inside a transaction. Most writes are one `apply(event)` call, but semantic collapses that require multiple decision events, such as tool-batch interrupt collapse, use `apply_many(events)` or an equivalent single transaction so observers and recovery never see half of the durable fact sequence. Run status, approvals, and tool invocation states have no direct write path beside it; atomicity is structural, not disciplinary.
_Avoid_: event store wrapper, dual-write store, message queue, repository

**DecisionEvent**:
A durable `RuntimeEvent` designed for state reconstruction: it records an orchestration decision or fact (batch planned, invocation started, approval resolved) whose typed fold yields run state. If answering a state question requires heuristics over incidental events, a decision event type is missing and should be added instead of the heuristic.
_Avoid_: log line, debug trace, transient delta, snapshot dump

**Projection**:
A derived, rebuildable read model (runs, tool invocations, approvals, conversation) computed by folding decision events. It is updated synchronously in the same transaction as the event append and can always be dropped and refolded; changing a projection's schema is not a data migration. Event type shapes are the only durable contract.
_Avoid_: authoritative state, source of truth, cache that can silently drift

**ToolInvocation**:
The per-tool-call state machine projection, keyed by `tool_call_id` and carrying `args_hash`, `effect`, and `risk`. Its states are proposed, awaiting_approval, approved, waiting_tool_result, denied, running, succeeded, failed, interrupted, and unknown. It is the unit the agent loop schedules and the unit crash recovery reasons about; it subsumes what other designs call a pending action or execution record. During an active interrupt, the tool/provider reports the outcome cooperatively; `effect` and `risk` are conservative fallback inputs when the runtime loses contact or receives no reliable outcome, not a replacement for the tool's own report. For non-dangerous/non-external-write tools cancelled by user stop with no more precise outcome, the fallback is interrupted, not failed.
_Avoid_: tool intent, pending action, execution log entry, queue item

**WaitingApproval**:
The actionable run state where one or more tool invocations require an explicit approval decision before the agent loop may continue. Re-entering this state restores the approval UI and routes the next input to approval handling first: approve resumes execution, deny records a model-visible denied observation, cancel terminates the whole run, and Ctrl+C only exits the current local interaction while preserving `WAITING_APPROVAL`. Free-form user text is not sent to the model until the approval is resolved.
_Avoid_: ordinary user prompt, interrupted run, model-visible notice, paused run

**WaitingToolResult**:
The resumable invocation state for an external/client-executed tool that has been approved by policy but is intentionally waiting for an out-of-process result to be submitted. It is normal control flow and resumes by appending a tool completion event, unlike `UnknownOutcome`, which is crash recovery for an indeterminate side effect. Passive UI disconnects and local Ctrl+C only exit the current waiting UI/subscription and leave the run in `WAITING_TOOL_RESULT`; re-entry restores the wait. First-version interrupt handling does not abandon this wait implicitly. A future explicit abandon-wait control would need to record a model-visible tool observation before continuing.
_Avoid_: unknown outcome, failed tool, pending approval, background execution, interrupted active work

**ToolBatch**:
The set of tool calls produced by one assistant turn, opened by `tool.batch_planned` and closed by `tool.batch_closed` only when every invocation in it has a model-visible observation. At most one batch is open per run; an open batch is the run's resume point after approval, pause, or crash. If a user interrupt abandons the current turn, remaining invocations that have no observation must receive interrupted/skipped observations even when the currently running invocation later becomes unknown. The batch closes before `RunInterrupted` only when all observations are present, and that closure plus the user-stop notice and interruption fact must be committed as one ledger transaction; if the active invocation outcome is unknown, the run pauses for recovery instead of pretending the batch was cleanly interrupted, but the rest of the batch is already abandoned and must not execute after recovery.
_Avoid_: parallel task queue, message group, tool cache

**ContextSnapshot**:
The frozen, hash-level proof of what one model call saw: message, tool, preamble, and model-config hashes plus counts, recorded on `step.started`. It answers "why did the model do that" by proving whether two builds saw the same input, without persisting the full prompt.
_Avoid_: full prompt dump, persisted system prompt, message history

**UnknownOutcome**:
The terminal-pending invocation state for an external-write tool that was started but whose completion was never recorded because the runtime crashed mid-flight, with no human in the loop. The side effect may or may not have happened, so it must be resolved by a human decision and never auto-retried; resolution appends the human-confirmed completion event. It is crash-recovery only: an interactive `Interrupt` instead terminates the tool and records an `interrupted` completion observation routed back to the live conversation, never this offline-resolve gate.
_Avoid_: failed, retryable error, timeout, denial, interrupted-by-user

**AGUITransport**:
The boundary that adapts an already-constructed `AgentRuntime` to a Web (AG-UI / CopilotKit) frontend, as knuth-cli adapts one to a terminal. It never builds a runtime and never decides server-side tools, prompt, or policy — those are agent policy and belong to the knuth-im host/runtime factory. It may own the AG-UI client-tool provider type because that is protocol adaptation, but the host explicitly registers that provider in the runtime and passes the same provider to `create_app(runtime, client_tool_provider=...)`. It observes runtime events and translates them to the AG-UI event vocabulary, routes explicit control intent to `RuntimeControl` or live `RunSession` operations, and answers read-only ledger queries. Its streaming response is only a subscription: passive SSE disconnects unsubscribe and do not interrupt or pause a run. A transport or host live manager, not `AgentRuntime`, owns routing to the active `RunSession`; UI stop sends an `InterruptSignal`, and re-opening the same run attaches to the live invocation or restores the latest actionable state rather than starting a duplicate invocation. When `RUNNING` is removed from resumable statuses, this live manager owns the replacement attach/reentry path so RUNNING runs do not fall through to ordinary resume errors. If the live manager force-cancels an invocation after deadline, it must call runtime recovery/control to complete a conservative durable outcome instead of dropping a zombie RUNNING session. It owns no agent policy, holds no durable run state of its own, and leaks no HTTP/AG-UI concept back into the runtime; the runtime may grow only additive, audience-neutral seams to serve it. Its concrete adapters and protocol mapping live in the knuth-agui package and ADR-006, not in this glossary.
_Avoid_: runtime extension, agent policy owner, runtime factory, control authority, server tool provider owner, CopilotKit/HTTP concept inside runtime, Node CopilotRuntime

**Observation**:
The model-visible text of a finished tool invocation — what the next model request reads as the `tool_result`. It is mandatory and self-contained; an `Artifact` may hold the full raw output behind it, but the observation is never a rehydration of that raw blob.
_Avoid_: raw tool output, observation preview, rehydrated content, offloaded result

**ObservationCondensation**:
Shrinking an `Observation` for context economy while keeping the tool-call / tool-result structure valid. It is purely a context-size concern and never a path for clearing secrets. Either a `SelfCondensingTool` condenses its own observation (and is then exempt), or a pluggable middleware backend condenses observations that arrived un-condensed; the runtime never re-condenses a self-condensed observation.
_Avoid_: redaction, secret masking, conversation compaction, generic truncation

**Redaction**:
Masking secrets — keys, tokens, credentials — before anything durable or model-visible is produced. It runs on event append and on artifact write and is irreversible by design. Distinct from `ObservationCondensation`, which only trims size and must never be relied on to clean secrets.
_Avoid_: context trimming, observation condensation, size limit, headroom excerpt

**SelfCondensingTool**:
A tool that archives its full output to the `ArtifactStore` and returns an already-condensed `Observation`, embedding the artifact's local filesystem path so the model can query it with ordinary shell tools. It marks the result so `ObservationCondensation` skips it; the runtime trusts the tool to know the best shape for its own data and applies no per-result fallback.
_Avoid_: streaming tool, raw-output tool, offload-only tool, preview tool

**Artifact**:
An immutable blob of a tool's full output, always materializable as a local filesystem path so the model can inspect it with `grep`, `jq`, `find`. Events reference it by id only; its bytes live in the `ArtifactStore`, never inline in the ledger, and secrets are masked on write.
_Avoid_: ledger side blob, SQL row, inline tool output, rehydration source

**ArtifactStore**:
The standalone, runtime-independent store that owns artifact bytes. Filesystem-backed today (a future general storage protocol may put the bytes on remote storage), but it must always resolve a `(run_id, artifact id)` pair to a local filesystem path so generic shell tools keep working — a per-run manifest maps ids to files, because an id alone does not encode its run or extension. It is not part of the ledger; the ledger — SQL or in-memory — only references artifacts by id.
_Avoid_: ledger side store, blob column, runtime-owned storage, database table
