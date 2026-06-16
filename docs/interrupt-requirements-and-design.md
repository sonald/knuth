# 可中断运行机制需求与设计

状态：Implemented
日期：2026-06-16
依据：[ADR-007](decisions/ADR-007-interrupt-signal-and-reentry.md)、[CONTEXT.md](../CONTEXT.md)、[resume-interrupt-findings.md](resume-interrupt-findings.md)

本文把 ADR-007 的架构决策展开成可实现的需求与设计。ADR 只记录为什么这么做；本文记录第一版应做什么、各层如何配合、哪些地方参考 Claude Code、如何验收。

## 目标

建立一套端到端可中断机制，使 Knuth 在 CLI、AG-UI/IM、runtime、llmd、toold 之间有一致的控制语义：

- Ctrl+C / UI stop 可以停止 active model/tool work，而不是把用户停止误写成 `PAUSED`。
- approval prompt / client tool result wait 这类 waiting 状态下的 Ctrl+C 只退出本地交互，不改变 durable run 状态。
- 重新进入交互模式时优先恢复 actionable run，例如重弹 approval，而不是要求用户记住 run id 手动 approve/resume。
- passive transport disconnect 不等于 interrupt；显式 stop 才产生 interrupt。
- tool/model 都能收到同一类 `InterruptSignal`，在自己的安全点协作式返回 outcome。
- durable ledger 只在明确 safe point 写入解释事实，避免把任意 cancellation site 当成状态转移依据。

## 参考 Claude Code 的地方

我们参考 Claude Code 的机制，但不照搬它的 in-memory React/Node 结构。Knuth 的落点必须经过 durable ledger、typed RuntimeEvent 和 runtime/tool/provider 边界。

| Claude Code 机制 | 来源 | Knuth 借鉴 | Knuth 不照搬 |
|---|---|---|---|
| `AbortController`/`AbortSignal` 是 one-shot、sticky，并携带 `reason`；child controller 继承 parent reason。 | `/Users/siancao/work/readings/claude-code-exposed/src/utils/abortController.ts` | `InterruptSignal` 应是 one-shot sticky signal，带领域级 `reason`、`run_id`、`created_at`；model/tool/UI 都观察同一个语义对象。 | 不把 JS `AbortController` 当领域模型；AnyIO cancel scope 只是执行机制；不把 CLI/AG-UI 等 adapter 名称写入 durable schema。 |
| cancel keybinding 先看是否有 active task，再看队列；不同 overlay/dialog 拥有自己的 cancel 处理。 | `/Users/siancao/work/readings/claude-code-exposed/src/hooks/useCancelRequest.ts` | Knuth driver 也要按当前 run/input mode 分流：active work 才 interrupt，approval/waiting 由本地 UI 自己退出或恢复。 | 不引入一个 runtime “control parser” 概念；这是 CLI/transport input policy。 |
| permission dialog 下 `onCancel` 交给 permission request 自己处理；不是普通 prompt。 | `/Users/siancao/work/readings/claude-code-exposed/src/screens/REPL.tsx` | `WAITING_APPROVAL` 下下一条输入先解释为 approval decision，Ctrl+C 只退出本地 approval UI。 | 不把 approval Ctrl+C 写成 `run.interrupted`，也不注入 model notice。 |
| query 在 streaming abort 和 tool abort 两处单独处理，确保已有 tool_use 有匹配 tool_result，并按 reason 决定是否插入 interruption message。 | `/Users/siancao/work/readings/claude-code-exposed/src/query.ts` | Knuth 也需要 safe point：model abort、tool abort 分别转成 durable model/tool/run facts；`ModelVisibleNotice` 只按需写。 | 不用普通 `user.message` 表示合成提示；ledger 写 synthetic notice，推理投影为 `InferenceMessage.role=User`。 |
| `ShellCommand` 根据 abort reason 决定是否 kill；`reason === "interrupt"` 时可让调用方 background 并保留 partial output。 | `/Users/siancao/work/readings/claude-code-exposed/src/utils/ShellCommand.ts` | tool/provider 应自己根据 signal reason 和自身语义报告 `interrupted/succeeded/failed/unknown`。 | 第一版不要求实现 shell backgrounding；shell 是否 kill、如何记录 partial side effects 是 shell tool 自己的协作策略。 |
| remote session 用 control request 发送 interrupt。 | `/Users/siancao/work/readings/claude-code-exposed/src/remote/RemoteSessionManager.ts` | AG-UI/daemon stop 应走显式 control endpoint，路由到 live `RunSession.interrupt(...)`。 | SSE 断开不是 control request。 |

## 非目标

- 不设计完整 `/cancel`、run 删除、长期后台任务管理。
- 不实现 shell command backgrounding；可作为后续工具增强。
- 不引入 runtime 内部全局 session registry。
- 不把 client tool manifests 进一步 ledger-backed；这仍是 `overlay_providers` 删除后的独立后续问题。
- 不把所有 interrupt 都转成 model-visible notice。

## 当前实现差距

当前代码里需要修正的主要差距：

- `RunStatus` 没有 `INTERRUPTED`。
- `ToolInvocationCompletedDraft.outcome` 只有 `succeeded/failed/denied`，`ToolInvocationStatus` 没有 `INTERRUPTED`。
- `InferenceAborted` 在 runtime loop 里会写 `RunPausedDraft`，需要改为 interrupt safe point 语义。
- `AgentRuntime.pause()` 目前被 CLI Ctrl+C 和 AG-UI `/pause` 当作 stop 使用，语义错误。
- `RunSession` 没有 live interrupt handle。
- `ToolRuntimeContext` 没有 interrupt signal，`ToolBroker.execute()` 只能返回 `ToolResult(success/error)`，无法表达 `interrupted/unknown`。
- CLI `_read_line` 用 `anyio.to_thread.run_sync(request.done.wait, abandon_on_cancel=True)` 等待一个 thread event，Ctrl+C 后可能留下非 daemon AnyIO worker 等待，导致进程退出卡住。
- `run_resume()` 不走统一 interruptible driver。
- `knuth-agui` 的 `/agent` 仍在 `StreamingResponse` generator 内 `async with RunSession`，passive SSE disconnect 会取消 session。
- `reconstruct_messages_from_events()` 还不能投影 `ModelVisibleNotice`。

## 需求

### R1. Durable 状态与事件

- 新增 `RunStatus.INTERRUPTED`。
- 新增 durable event `run.interrupted`，至少包含：
  - `reason: "user_stop" | "queued_user_prompt" | "timeout" | "shutdown" | "hook_stop" | "runtime_stop"`
  - `active_phase: "model" | "tool" | "loop" | "unknown"`
  - `message: str | None`
- `run.interrupted` 不记录 `cli` / `agui` / `im` / `daemon` 这类 adapter 或业务 Agent 名称。入口来源属于上层 host 的 telemetry/debug 语义；core durable event 只记录领域原因。
- `queued_user_prompt` 在 v1 只是保留 reason，表示未来“新用户输入取代当前 active turn”的能力；当前 CLI/AG-UI v1 不产生它，active run 上带 prompt 的请求仍按 R9 返回 409 或 attach。
- `run.interrupted` 只允许从 active status 写入，主要是 `CREATED/RUNNING`；不允许从 `WAITING_APPROVAL`、`WAITING_TOOL_RESULT`、terminal status 写入。
- `run.interrupted` 不能和 open tool batch 并存。若 interrupt 发生在 tool batch 内，必须先让 batch 中每个缺少 observation 的 invocation 得到 model-visible observation，并写入 `tool.batch_closed`；之后才允许写 `run.interrupted`。
- tool-batch interrupt safe point 是一个语义原子操作：abandon/interrupted observations、`tool.batch_closed`、必要的 `conversation.notice`、`run.interrupted` 必须通过 `RunLedger.apply_many(...)` 或等价单事务机制一起提交。不能用多个独立 `ledger.apply(...)` 事务拼接，否则 crash/force-stop 可能落在中间，让 run 失去 interrupt 事实并在 recovery 后静默续跑。
- 若 active tool invocation 的 outcome 是 `UNKNOWN`，本轮不能写 clean `run.interrupted`，而应进入 `PAUSED` / recovery path，由人工或恢复流程处理未知副作用。
- 被 interrupt 放弃的 model attempt / tool turn 不应消耗 `max_turns` 预算。`run.steps` / `StepStarted.index` 继续作为单调 attempt counter，包含被中断的 attempts，不能 rollback；`max_turns` 必须改用独立的 completed/committed model-turn count，而不是复用 `run.steps`。连续 Ctrl+C 不能把 run 推到 `max_turns_exceeded`。
- `RunStatus.INTERRUPTED` 不属于 `_RESUMABLE_STATUSES`。
- `_RESUMABLE_STATUSES` 应集中定义在一处，且不包含 `RUNNING`；`RUNNING` 是 live attach 或 explicit recovery 的对象，不是 `resume()` 的对象。
- 移除 `RUNNING` 的同时必须由 R9 live manager 接管 `RUNNING` 的 attach/reentry；不能只改 `_RESUMABLE_STATUSES`，否则 AG-UI 无 prompt 打开 RUNNING run 会误落到 “cannot be resumed”。
- `RuntimeControl.resume()` 必须拒绝 `INTERRUPTED`。
- `UserMessageDraft` reducer 允许从 `INTERRUPTED` 追加新用户输入，使 `continue_run()` 可以开启下一轮。

### R2. Tool outcome

- `ToolInvocationCompletedDraft.outcome` 扩展为 `succeeded | failed | denied | interrupted`。
- `ToolInvocationStatus` 新增 `INTERRUPTED`。
- interrupted tool completion 必须有 model-visible observation；该 observation 应说明工具被中断，且由 tool/provider 决定是否提示 partial side effects。
- `user_stop` 落在 tool batch 时，语义是停止整个当前 invocation / turn，不是只停止一个 tool 后继续执行同一批剩余 tool。`interrupted` observation 是给下一次 context reconstruction 用的，不是让旧 batch 继续跑的许可。
- 一旦本轮收到 `user_stop`，batch 中尚未开始、尚未生成 observation 的 invocation 应写入 `interrupted`/abandoned observation，明确说明这些 tool 因用户停止本轮而未执行。这条由 `user_stop` 触发，不依赖 active invocation 最后返回 `interrupted` 还是 `UNKNOWN`。
- `UNKNOWN` 仍用于无法确定副作用结果的恢复路径；不能把 unknown 编码成 failed 或 interrupted。
- `tool.batch_closed` 仍要求所有 invocation 都有 model-visible observation。`UNKNOWN` 不满足关闭条件；如果 active invocation 出现 `UNKNOWN`，本轮进入 `PAUSED`，不能伪造成已干净中断。但未开始的剩余 invocation 仍必须先被 abandoned，避免人工 resolve 后 resume 时继续执行用户已经停止的旧 batch。

### R3. `ModelVisibleNotice`

- 新增 durable synthetic notice event，推荐事件名 `conversation.notice`。
- 字段建议：
  - `kind: "interrupted" | "runtime"`
  - `content: str`
- reducer 不改变 run status。
- `reconstruct_messages_from_events()` 把它投影为 `InferenceMessage(role=InferenceRole.USER, content=content)`。
- 它不能复用 `UserMessageDraft`，不能改变 run 的 authorship。
- 不新增 notice 自己的实体身份；stored event 已经有 `id` / `seq` / `created_at`。第一版也不记录 `source_event_id`，需要时可通过相邻 durable events 或后续显式 provenance 设计补充。
- `conversation.notice` 只能插入在 provider conversation 合法的位置。若上一条 assistant message 含 tool calls，必须先保证对应 tool observations 已写入并关闭 batch，才能插入 user-role notice；不能把 notice 插在 assistant tool_use 与缺失 tool_result 之间。
- notice policy：active work 被 `user_stop` 放弃后，写一条简短 `conversation.notice` 表达“上一轮由用户停止，不要默认重试旧动作”。active model work 被放弃时，notice 承载被丢弃的 assistant partial 事实；active tool batch 被 Ctrl+C 时，仍先写 tool observations 并关闭 batch，再在合法 conversation boundary 写 notice。waiting 状态本地退出不写 notice。

### R4. `InterruptSignal` 原语

新增 runtime-neutral 原语，建议放在 `knuth-core` 或 `knuth-runtime` 的中性模块。第一版需要：

```python
class InterruptSignal(Protocol):
    @property
    def interrupted(self) -> bool: ...
    @property
    def reason(self) -> str | None: ...
    async def checkpoint(self) -> None: ...
```

实际实现建议：

- `InterruptController` 持有 sticky signal。
- `interrupt(reason)` 只能第一次生效，后续调用不覆盖原 reason。
- 支持创建 child signal；parent interrupt 传播到 child，child interrupt 不反向影响 parent。
- 支持注册少量 cleanup callback，但 callback 不能写 ledger；ledger 写入只能在 safe point。
- `checkpoint()` 第一版不抛 cancellation exception；它只让出控制权并让调用方在返回后检查 `interrupted/reason`。抛取消或唤醒阻塞 await 是独立的执行机制。
- 对 poll-friendly 工作（stream chunk loop、工具内部循环），调用方用 `checkpoint()` / `interrupted` 协作返回 outcome。
- 对 single-blocking await（模型 TTFT、子进程、长网络 read），必须把 signal 绑定到 AnyIO cancel scope、provider abort API 或 subprocess terminate 来唤醒 await；单靠轮询无法中断。
- AnyIO cancel scope 可以绑定到 signal，用于唤醒阻塞 await，但不能绕过 safe point 直接写状态。捕获 backing cancellation 后，safe point 的 durable ledger writes 必须放在 `anyio.CancelScope(shield=True)` 或等价 shield 中，避免取消展开期间把落账也取消掉。
- 第一版 primitive 应提供统一 wakeup 接线：`InterruptController` 持有 sticky `anyio.Event` 供 `checkpoint()` / `wait_interrupted()` 使用，并提供 scoped wakeup registration，让 single-blocking operation 在进入阻塞 await 前注册当前可取消 scope 或 abort callback，退出时 unregister。`RunSession.interrupt()` 只触发 controller；具体 await 的唤醒由注册点完成。

推荐 reason vocabulary：

- `user_stop`：Ctrl+C 或 UI stop，停止当前 active work。
- `user_cancel`：保留给未来 dialog/prompt 取消进入统一 signal path 的能力。v1 approval prompt Ctrl+C 只退出本地 UI，不产生此 reason。
- `queued_user_prompt`：保留给未来“新用户输入取代当前 active turn”的能力；类似 Claude Code 的 `reason === "interrupt"`，通常不需要额外 notice。v1 不产生此 reason。
- `timeout`：runtime 或 host timeout。
- `shutdown`：宿主或进程正在关闭。

### R5. `RunSession` live interrupt

- `RunSession` 创建并持有 invocation-scoped `InterruptController`。
- `RuntimeInvocation` 暴露 `interrupt_signal` 给 loop、llmd runtime options 和 tool runtime context。
- `RunSession.interrupt(reason="user_stop") -> bool` 触发 signal，并尝试唤醒当前 model/tool await。
- `RunSession.interrupt(...) -> bool` 的返回值只表示本次调用是否把 sticky signal 从未触发翻转为触发；它不保证当时仍有 active work，也不保证 durable state 已经进入 `INTERRUPTED`。
- `RunSession.interrupt(...)` 只负责 live signal 和 wakeup；`run.interrupted`、`tool.completed`、`tool.unknown` 等 durable 写入只能由 agent loop 的 safe point 执行，且取消路径上的落账必须 shield。
- `RunSession` 只负责当前 invocation；它不注册到 `AgentRuntime` 的全局列表。
- `RunSession.__aexit__` 因普通 context 退出取消 task group 时，不应自动写 `run.interrupted`；只有显式 signal + safe point 才写。
- safe point catch 到 cancellation 时必须先检查 `signal.interrupted`。若 signal 已触发，才把 cancellation 解释成 interrupt outcome 并 shield 落账；若 signal 未触发，说明这是普通 teardown、task-group cancellation 或其它上层取消，必须原样重抛/传播，不能伪造 `run.interrupted`。
- force stop 是 driver/supervisor 的第二阶段，不保证 durable clean outcome。

### R6. Model path

- `InferenceRuntimeOptions.abort_signal` 改为接收 `InterruptSignal` 或兼容 adapter。
- llmd 在 request 前、stream chunk 间检查 signal；模型请求的初始 await / TTFT 必须可被 signal 唤醒，不能等到第一个 chunk 才观察中断。
- signal 触发时产生 `InferenceAborted(reason=<signal.reason>)`，不是 `InferenceFailed`。
- `InferenceAborted.reason` 必须透传 `signal.reason`，不能硬编码为 `"abort_signal"`，因为 runtime 需要用 reason 区分 user stop、timeout、shutdown 或 provider 自身 abort。
- runtime loop 收到 `InferenceAborted`：
  - emit `ModelAbortedDraft(step_id, reason)`
  - 若 reason 是 active work stop，emit `RunInterruptedDraft(active_phase="model", ...)`
  - 根据 R3 notice policy 决定是否写 `conversation.notice`；active model abort 通常需要 notice，因为被丢弃的 assistant partial 不会进入 durable conversation。
  - return `RunStatus.INTERRUPTED`
- provider/network error 仍走 `ModelFailedDraft` + `RunFailedDraft` 或其它非 interrupt 路径。

### R7. Tool path

`ToolRuntimeContext` 增加 `interrupt_signal`。不要让工具直接接触 `RunSession` 或 ledger。

分流以“当前进程是否正在执行该 tool/provider 的 active work”为准，而不是以工具 effect/risk 为准。client/external tool result wait 已经是 `WAITING_TOOL_RESULT`：当前进程没有正在执行的 tool 可中断，Ctrl+C / passive disconnect 只退出本地等待 UI/订阅。`DANGEROUS` 的本地 shell tool 则仍是 active local tool，应该收到 signal 并按自己的安全点报告 outcome。`effect` / `risk` 只在 runtime 失去可靠 outcome 时做保守 fallback。

建议不要继续让 `ToolBroker.execute()` 只返回 `ToolResult`，因为 `UNKNOWN` 不是一种 tool result，也不应生成 tool_result message。建议引入执行层结果：

```python
class ToolExecutionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"

class ToolExecutionResult(KnuthModel):
    outcome: ToolExecutionOutcome
    result: ToolResult | None = None
    observation: str | None = None
    reason: str | None = None
    tool_status: str | None = None
```

映射规则：

- `SUCCEEDED/FAILED`：写 `tool.invocation_completed(outcome="succeeded"|"failed")`。
- `INTERRUPTED`：写 `tool.invocation_completed(outcome="interrupted")`，必须有 observation。
- `UNKNOWN`：写 `tool.invocation_marked_unknown(reason=...)`，然后 run `PAUSED`，等待人工恢复。
- provider 抛异常且 signal 未触发：按 failed 处理。
- provider 抛 cancellation 且 signal 已触发，但没有可靠 outcome：按 `effect/risk` 保守 fallback；`DANGEROUS/EXTERNAL_WRITE` 走 `UNKNOWN`，其它默认写 `interrupted` observation。用户停止导致的取消不是工具自身失败，除非 provider 明确报告确定错误。

内置 shell tool 的第一版策略建议：

- 收到 `user_stop` 后向子进程发送温和终止信号，短暂 grace 后 force kill。
- 若子进程已退出并能收集 stdout/stderr，返回 `INTERRUPTED`，observation 明确写“命令被用户中断，部分输出如下，命令可能已经产生部分副作用”。
- 若无法确认子进程状态或 runtime 失联，返回/恢复为 `UNKNOWN`。
- 不在第一版实现 Claude Code 式 backgrounding；未来可以让 shell tool 根据 reason 选择 background 并返回 model-visible background task observation。

### R8. CLI input 和交互重入

CLI 必须只有一套 foreground driver，用于 `run_interactive`、`run_single`、`run_resume`。

输入读取要求：

- 不再把 AnyIO worker thread 阻塞在 `threading.Event.wait()` 上。
- 保留单 stdin reader thread 读取 tty，以避免多个 worker 同时读 stdin。
- reader thread 通过 AnyIO token/portal 通知 async task，例如 set `anyio.Event`，async task 直接 `await event.wait()`。
- Ctrl+C 取消等待时，不留下非 daemon worker。
- approval prompt Ctrl+C 返回“本地 approval UI 被取消/退出”的结果，不写 `run.interrupted`。

交互重入要求：

- CLI 启动或回到 top-level 时先查 actionable run。
- 单个 actionable run：
  - `WAITING_APPROVAL`：重弹 approval prompt。
  - `WAITING_TOOL_RESULT`：显示等待 external/client tool result。
  - `PAUSED`：提示 resume。
  - `RUNNING`：若当前进程持有 live session，attach；否则提示“当前进程无法 attach”。在没有 run lease / heartbeat 前，不能自动 recovery，因为另一个进程可能仍在执行该 run；recovery 必须由显式命令或显式确认触发。
- 多个 actionable runs：列出并要求用户选择或提供 run id。
- `INTERRUPTED/SUCCEEDED`：普通 prompt，下一条输入走 `continue_run`。

### R9. AG-UI live manager

`knuth-agui` 需要 host/transport-owned live manager；它不是 `AgentRuntime` 的一部分。

建议结构：

```text
AGUI request
  -> validate threadId/run_id
  -> register client tools if present
  -> LiveRunManager.start_or_attach(run_id, prompt?)
       - if no live session: create RunSession and internal fanout listener
       - if live session exists and prompt is absent: attach subscriber
       - if live session exists and prompt is present: return 409 in v1
  -> SSE subscriber receives fanout events
```

要求：

- passive SSE disconnect 只移除 subscriber，不退出 `RunSession`。
- live session 到达 `WAITING_APPROVAL`、`WAITING_TOOL_RESULT`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`INTERRUPTED` 后，从 live map 移除。
- 重新打开同一个 run：
  - 有 live session：attach。
  - 无 live session：按 durable status 走 start/continue/resume/reentry。
- UI stop endpoint 不调用 `runtime.pause()`；新增 `/interrupt` 或 `/stop`，路由到 live manager。
- live manager 对 graceful interrupt 必须设置 deadline。deadline 到期后可以 force-cancel 当前 live task / child process；不能让不可达 safe point 的工具无限占用 live session。若 durable state 未得到 clean outcome，交给 recovery / `UNKNOWN` 处理。
- AG-UI/host live manager 在 deadline force-cancel 后不能只把 session 从 live map 移除。它必须调用 runtime 提供的 recovery/control primitive，把本进程失联的 active invocation 保守落成 `UNKNOWN + PAUSED`，或完成已经开始的 interrupt safe-point collapse；durable 写入仍走 runtime/ledger，不由 AG-UI 直接伪造 tool 状态。
- 若 stop 目标没有 live session：
  - waiting 状态返回当前 status，不写 interrupt。
  - terminal status 返回 409 或 idempotent no-op，二者择一并测试固定。

### R10. Force stop

- CLI 第一次 Ctrl+C：graceful interrupt。
- CLI 第二次 Ctrl+C：退出当前 process 或 force stop child process；不伪造 durable clean outcome。
- graceful interrupt 必须有 host/driver deadline；deadline 到期等价进入 force path，不等待用户永远按第二次 Ctrl+C。
- AG-UI force stop 可作为未来 endpoint；第一版 UI stop 可以只有 graceful API，但 host live manager 仍必须有 deadline 和内部 force cleanup，避免 live session 泄漏。
- recovery 命令负责把离线遗留 `RUNNING` / `RUNNING tool invocation` 转为 `PAUSED/UNKNOWN`；host/live manager 对自己 force-cancel 产生的遗留状态负有在线收口责任，不能依赖 CLI-only 手动 recovery。

## 状态分流矩阵

| 当前状态/阶段 | Ctrl+C / UI stop | passive disconnect | 下一条普通用户输入 | resume |
|---|---|---|---|---|
| active model stream | 唤醒/abort request；`InferenceAborted(reason)` -> shielded safe point -> `run.interrupted` -> `INTERRUPTED` | CLI 无；AG-UI unsubscribe，不 stop | `continue_run` | 拒绝 |
| active local tool | signal 传给 tool；先 abandon 未开始的剩余 invocations；若 active outcome 确定则补齐 observations、关闭 batch、再 `run.interrupted`；若 active outcome `UNKNOWN` 则保留 unknown recovery、run `PAUSED` | AG-UI unsubscribe，不 stop | 若 run `INTERRUPTED/SUCCEEDED` 可 `continue_run`；`PAUSED/UNKNOWN` 先 recovery | 拒绝 clean resume；只允许 recovery |
| `WAITING_APPROVAL` | 退出本地 approval UI，status 不变 | unsubscribe | 不进 model，先处理 approval | approval resolved 后 resume |
| `WAITING_TOOL_RESULT` | 退出本地 waiting UI，status 不变 | unsubscribe | 不进 model，等 result 或显式 abandon | result submitted 后 resume |
| `RUNNING` 但当前进程无 live session | 不自动 recovery；提示无法 attach，要求显式 recovery/确认 | no-op | 不直接进 model | 拒绝普通 resume |
| `PAUSED` | no-op 或提示已 paused | no-op | 不直接进 model，先提示 resume/continue choice | 允许 |
| `INTERRUPTED` | no-op | no-op | `continue_run` | 拒绝 |
| `SUCCEEDED` | no-op | no-op | `continue_run` | 不需要 |
| `CANCELLED/FAILED` | no-op | no-op | 拒绝或新 run | 拒绝 |

## 实现阶段

### Phase 1：core event 和 projection

文件范围：

- `packages/knuth-core/src/knuth/core/types.py`
- `packages/knuth-core/src/knuth/core/invocations.py`
- `packages/knuth-core/src/knuth/core/runtime_events.py`
- `packages/knuth-runtime/src/knuth_runtime/ledger.py`
- `packages/knuth-runtime/src/knuth_runtime/context.py`
- event serialization tests

验收：

- `RunStatus.INTERRUPTED` 可存储、refold。
- `run.interrupted` 从 active 状态转 `INTERRUPTED`。
- `user.message` / `continue_run` 允许从 `INTERRUPTED` 继续。
- `resume` 从 `INTERRUPTED` 被拒绝。
- `tool.invocation_completed(outcome="interrupted")` 转 invocation `INTERRUPTED`，并满足 batch close 的 observation requirement。
- interrupt 发生在 open tool batch 时，所有缺失 observation 的 invocation 被补齐、batch 被关闭，然后才出现 `run.interrupted`。
- tool-batch interrupt collapse uses one `RunLedger.apply_many(...)` transaction; crash/force-stop cannot observe `batch_closed` without the corresponding notice/run interruption facts.
- interrupt 发生在 open tool batch 且 active invocation 变成 `UNKNOWN` 时，尚未开始的剩余 invocation 已被 abandoned；人工 resolve unknown 后不会继续执行旧 batch 的剩余 tool。
- `run.steps` remains a monotonic attempt counter, while `max_turns` uses a separate completed/committed turn count; interrupted attempts do not push the run to `max_turns_exceeded`。
- `conversation.notice` 投影为 user-role inference message。
- `conversation.notice` 不会插入到 assistant tool_use 和缺失 tool observation 之间。
- active tool batch Ctrl+C writes tool observations, closes the batch, then writes a short user-stop notice at a legal conversation boundary so the model does not default-retry the old action.

### Phase 2：interrupt primitive 和 RunSession

文件范围：

- `knuth-core` 或 `knuth-runtime` interrupt primitive 模块
- `packages/knuth-runtime/src/knuth_runtime/session.py`
- `packages/knuth-runtime/src/knuth_runtime/invocation.py`
- `packages/knuth-runtime/src/knuth_runtime/loop.py`

验收：

- `RunSession.interrupt()` 是 one-shot。
- `RunSession.interrupt()` returns whether it first triggered the sticky signal, not whether active work was actually interrupted.
- signal 进入 `RuntimeInvocation`、llmd runtime options、tool runtime context。
- `InterruptController` 提供统一 wait/wakeup 接线，single-blocking await 通过 scoped registration 被唤醒，调用点负责 unregister。
- active model abort 进入 `INTERRUPTED`，不再写 `PAUSED`。
- cancellation path 上的 safe point ledger writes 被 shield，取消展开不会吞掉 durable facts。
- cancellation catch 点用 `signal.interrupted` 区分 active interrupt 与普通 teardown；非 interrupt cancellation 不写 `run.interrupted`。
- session context 普通退出不自动伪造 interrupt。

### Phase 3：model 和 tool 协作式中断

文件范围：

- `packages/knuth-llmd/src/knuth_llmd/client.py`
- `packages/knuth-toold/src/knuth_toold/base.py`
- `packages/knuth-toold/src/knuth_toold/broker.py`
- built-in tools / CLI tools
- runtime loop tool execution path

验收：

- llmd 对 `InterruptSignal` 产出 `InferenceAborted(reason=<reason>)`。
- llmd 透传 `signal.reason`，不硬编码 `"abort_signal"`。
- 初始 request await / TTFT 可以被 interrupt 唤醒，不依赖首个 chunk 到达。
- tool context 收到 signal。
- 一个 fake long-running tool 可以在 signal 后返回 `interrupted`。
- 一个 fake dangerous tool 无可靠报告时进入 `UNKNOWN + PAUSED`。
- 一个非 dangerous/read-only tool 在 user stop cancellation 且无可靠结果时默认写 interrupted observation，而不是 failed。
- shell tool first-version interrupt observation 包含 partial side effects warning。

### Phase 4：CLI driver 和 reentry

文件范围：

- `packages/knuth-cli/src/knuth_cli/repl.py`
- CLI command entrypoints
- CLI tests

验收：

- `run_interactive`、`run_single`、`run_resume` 共享同一套 interrupt driver。
- approval prompt Ctrl+C 不写 `run.interrupted`，run 保持 `WAITING_APPROVAL`。
- 重新进入 interactive CLI 时重弹 pending approval。
- Ctrl+C 不再留下阻塞 AnyIO worker，不需要第二次 Ctrl+C 才退出。
- `resume` pending approval 时仍给出明确错误或转入 approval UI，不能裸崩。
- `RUNNING` 但当前进程无 live session 时不自动 recovery；没有 lease/heartbeat 前只能提示并要求显式 recovery/确认。
- `queued_user_prompt` 在 v1 不由 CLI 产生；输入新 prompt 不隐式打断 active run。

### Phase 5：AG-UI live manager

文件范围：

- `packages/knuth-agui/src/knuth_agui/app.py`
- new `knuth_agui/live.py`
- AG-UI tests
- IM app stop button wiring

验收：

- SSE disconnect 不取消 live run。
- `/stop` 或 `/interrupt` 能 interrupt active live session。
- graceful interrupt 超过 deadline 后 live manager 能 force cleanup，不无限保留 live session。
- deadline force-cancel 后，live manager 调用 runtime recovery/control primitive 落成 `UNKNOWN + PAUSED` 或完成 pending interrupt collapse，不留下 zombie `RUNNING`。
- active run 上新 prompt 返回 409 或 attach，不开第二个 invocation。
- 移除 `RUNNING` from resumable status 后，RUNNING attach/reentry 由 live manager 路由接管，不落到普通 resume 409。
- waiting approval 切换会话再回来，approval card/actions 仍可见。
- `/pause` 不再作为 UI stop 语义使用。

## 测试策略

基础命令：

```sh
uv run python -m unittest discover -s tests -v
uv run python -m compileall packages tests
git diff --check
```

需要新增的重点测试：

- ledger reducer/refold：
  - `run.interrupted` status transition；
  - `resume(INTERRUPTED)` rejected；
  - `continue_run(INTERRUPTED)` accepted；
  - `tool interrupted` closes batch only with observation；
  - interrupt during open batch writes observations for skipped invocations, closes batch, then writes `run.interrupted`；
  - interrupt during open batch plus active `UNKNOWN` abandons remaining unstarted invocations before `PAUSED` recovery；
  - interrupted model/tool attempts do not count against `max_turns`；
  - `conversation.notice` reconstruction as `InferenceRole.USER`。
  - `conversation.notice` is rejected or delayed when previous assistant tool calls lack matching observations。
  - `conversation.notice` is not duplicated when interrupted/abandoned tool observations already carry the interruption fact。
- runtime loop：
  - fake inference client yields `InferenceAborted(reason="user_stop")` -> `INTERRUPTED`；
  - `InferenceAborted.reason` preserves `InterruptSignal.reason`；
  - long pre-first-token inference await can be woken by interrupt；
  - fake tool sees signal and returns interrupted；
  - fake tool no reliable outcome with dangerous effect -> `UNKNOWN + PAUSED`。
  - fake tool no reliable outcome with read-only effect under user stop -> interrupted observation, not failed。
  - cancellation during safe point does not prevent shielded ledger writes。
  - teardown cancellation without `signal.interrupted` is propagated and does not write `run.interrupted`。
- CLI：
  - approval prompt Ctrl+C leaves `WAITING_APPROVAL`；
  - reentry restores approval prompt；
  - no blocked worker after Ctrl+C; this needs TTY/signal style test or a small integration harness, not only pure pipe test。
  - second process seeing `RUNNING` without local live session does not auto-recover another process's run。
  - active run plus new prompt does not produce `queued_user_prompt` in v1。
- AG-UI：
  - disconnect SSE while model/tool active, then attach/check run still active or reaches natural state；
  - explicit stop sends interrupt；
  - stop deadline forces live cleanup when a tool never reaches a cooperative safe point；
  - duplicate active prompt returns 409。
  - RUNNING attach path is handled by live manager after RUNNING is removed from resumable statuses。
- IM E2E：
  - `waiting approval -> switch conversation -> switch back`，approval card/actions 仍显示；
  - active generation stop 后状态显示 `INTERRUPTED`，下一条普通输入可继续。

## 成功标准

- 用户在 approval prompt 按 Ctrl+C 后，不需要外部 `approve`/`resume` 拼状态；重新进入能看到同一个确认问题。
- 用户在 active model/tool work 按 Ctrl+C 或 UI stop 后，run 进入 `INTERRUPTED` 或 tool-reported recovery path，不再误写 `PAUSED`。
- `PAUSED` 只出现在 runtime/system 可恢复暂停路径。
- SSE 断开不会杀 run。
- 所有实现路径都能从 ledger 状态解释，不依赖旧进程里残留的 incidental state。
- 全量 unit tests、compileall、diff check 通过；CLI/IM 的真实中断路径有 smoke 验证。

## 已确认的决策（实现版）

- `conversation.notice`：采用 `conversation.notice` 事件名。
- Shell tool：收到 `user_stop` 后向子进程的进程组发送 `SIGTERM`，短 grace（默认 2s）后 `SIGKILL`；新建会话组确保孙子进程（如 `sleep`）一并停止；返回 `interrupted` outcome，observation 携带 partial output 与副作用警告。
- `INTERRUPTED` 后继续：CLI 不提供 `/continue`，下一条普通用户输入原样作为新 prompt 走 `continue_run`（reducer 允许 `user.message` 从 `INTERRUPTED` 追加，`run.resumed(cause="user_message")` 翻回 `RUNNING`）。
- AG-UI active run 上新 prompt：返回 409（`DuplicateActivePromptError`），不排队。
- live-run lease / heartbeat：第一版不引入；没有 lease 前不自动 recovery `RUNNING`，由显式 `recover` 或 host live manager 的 deadline force-cancel 后调用 recovery 收口。
