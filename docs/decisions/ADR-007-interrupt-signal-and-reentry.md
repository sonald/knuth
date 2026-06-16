# ADR-007: 可中断运行与交互重入机制

## 状态
Accepted

## 日期
2026-06-16

## 实现规格

详细需求、分层设计、Claude Code 参考点、实现阶段和验收矩阵见 [可中断运行机制需求与设计](../interrupt-requirements-and-design.md)。

## 背景

一次 CLI 交互复现暴露了两组问题：

- `WAITING_APPROVAL` 后跨进程 `resume` 时，已批准的 `shell` 工具曾经因为 registry 冷启动而找不到。
- 用户在 approval prompt 或后续 tool approval 流程中按 Ctrl+C 后，CLI 可能卡住到解释器退出阶段，需要第二次 Ctrl+C 才能结束。

第一组问题已经通过删除 `overlay_providers`、让工具 provider 注册进入 runtime-wide registry、并让 broker operation 自满足 registry 可用性来收口。剩下的问题不只是一个 `KeyboardInterrupt` bug，而是一整套控制语义没有统一：有的入口把 Ctrl+C 当 pause，有的入口裸跑 `session.result()`，有的 UI 断连会取消 run，有的本地输入读取用 abandon 语义却没有释放等待者。

Knuth 的 durable run / ledger 已经按跨进程、可恢复的方向设计；但中断、暂停、审批、客户端工具等待、live session、UI 订阅仍混在一起。需要一套所有层都能遵守的可中断机制：信号来源归一、信号沿运行路径传递、模型和工具在安全点协作停止、runtime 只在确定的安全点写 durable 事实，CLI / Web / daemon 各自把本地交互生命周期映射到同一套领域语义。

本 ADR 修正早期“Ctrl+C 进入 `PAUSED`”的方向：用户 stop 当前 active work 应该是 `INTERRUPTED`，不是可恢复 pause。`PAUSED` 只保留给 runtime / system 判断同一段未完成工作可以或必须被恢复的情况。

## 决策

### 1. `InterruptSignal` 是领域信号，cancel scope 只是执行机制

Knuth 引入 `InterruptSignal` 作为 live、normalized、one-shot 的控制信号。它可来自 Ctrl+C、UI stop、daemon command、transport 控制、timeout 或 blocking hook，但进入 runtime/model/tool/UI 层后使用统一语义。

AnyIO `CancelScope` 可以作为实现执行取消的机制，但不能作为领域原语暴露到所有层。领域层传递的是 `InterruptSignal`；底层使用 cancel scope、subprocess terminate、provider abort API 或其他机制只是执行细节。

`InterruptSignal` 不是 durable history。它只存在于当前 `RunInvocation` 的 live 执行中。只有当 runtime 到达 `InterruptSafePoint`，并能解释当前工作被如何处理时，才写入 durable runtime event。

durable interrupted fact 只记录领域原因，例如 user stop、timeout、shutdown、hook stop 或 runtime stop；不把 `cli`、`agui`、`im`、daemon 名称或未来业务 Agent 名称写进 core event schema。这些入口来源属于 host/adapter 的 telemetry 或 debug 语义。

### 2. 只有 `InterruptSafePoint` 能把 live signal 转成 durable 事实

异步取消可能发生在任意 await 处，但 ledger 不能在任意 exception site 解释运行结果。runtime 必须定义少量 `InterruptSafePoint`：

- model request 被协作式 abort 后；
- tool/provider 返回确定的 interrupted / succeeded / failed / unknown outcome 后；
- agent loop 在开始下一段工作前发现 signal 已触发；
- driver 在等待 approval / client tool result 这类非 active work 状态时决定只退出本地 UI。

只有这些点可以追加 `run.interrupted`、`tool.completed(outcome="interrupted")`、`model.aborted(reason=<reason>)`、`tool.unknown` 或其它 durable fact。普通 await 被取消、线程被放弃、SSE 断开、stdin reader 退出都不是 ledger 解释点。

实现上，安全点不是单个普通 await，而是一段受保护的解释流程：观察到协作 outcome 或捕获 backing cancellation 后，runtime 必须在 shielded 区域内把确定事实写入 ledger。对于模型首 token 前等待、子进程等待、长网络 read 这类 single-blocking await，轮询检查点不够，driver/provider 必须用 cancel scope、provider abort API 或子进程终止机制把 await 唤醒；随后仍由 safe point 决定 durable outcome。

被用户 interrupt 放弃的 attempt 不应消耗 `max_turns` 预算。`StepStarted` 这类 audit fact 可以保留为尝试记录，`run.steps` / `StepStarted.index` 继续单调记录所有 attempts，不能 rollback；turn budget 应按独立的 completed/committed model-turn count 计算，不能让连续 Ctrl+C 把 run 推到 `max_turns_exceeded`。

### 3. `INTERRUPTED`、`PAUSED`、`CANCELLED` 分工

新增或采用以下语义边界：

- `INTERRUPTED`：当前 active `RunInvocation` 被用户或控制面请求停止，active work 已被放弃；run 仍然存在，但旧 model request / tool batch 不可 replay，不可 `resume`。
- `PAUSED`：runtime / system 认为同一段未完成工作可以或必须被恢复，例如 crash recovery、unresolved `UnknownOutcome`、model/provider abort 的可恢复路径、blocking hook 暂停。
- `CANCELLED`：整个 run 被显式终止，不再 resumable，也不再 continuable。

`resume` 只用于已有未完成控制点，例如 `PAUSED`、`WAITING_APPROVAL`、`WAITING_TOOL_RESULT`。`continue_run` 表示追加新的用户输入并开启新一轮 invocation。`INTERRUPTED` 后不能 `resume`，只能通过新的用户输入或未来明确的 runtime-controlled follow-up 继续。

若 interrupt 发生在 open tool batch 中，`user_stop` 的语义是停止整个当前 invocation / turn，而不是停止一个 tool 后继续执行同一批剩余 tool。runtime 必须先把尚未开始、缺少 observation 的 invocation 写成 abandoned/interrupted observation，避免它们在后续 resume 中继续执行。若 active tool outcome 确定，则补齐 observations、关闭 batch，之后才允许写 `run.interrupted`；若 active tool outcome 无法确定，应进入 `PAUSED` / recovery path，而不是写 clean `INTERRUPTED`，但剩余未开始 invocation 仍已被放弃。

tool-batch interrupt collapse 必须通过 `RunLedger.apply_many(...)` 或等价单事务提交完成。abandoned/interrupted observations、`tool.batch_closed`、必要的 `conversation.notice` 和 `run.interrupted` 是一个语义原子操作，不能用多个独立 ledger transaction 拼接，否则 force stop 或 crash 可能留下 batch 已关闭但 interruption fact 缺失的中间态。

### 4. waiting 状态不是 active work interrupt

`WAITING_APPROVAL` 与 `WAITING_TOOL_RESULT` 不等同于 active model/tool work。

`WAITING_APPROVAL` 下，重新进入 run 时应恢复 approval UI，而不是先给普通 prompt。下一条输入先解释为 approval decision：

- approve：解决 approval，后续由调用方显式 resume；
- deny：写入 model-visible denied tool observation，后续 resume agent loop；
- cancel：终止整个 run，进入 `CANCELLED`；
- Ctrl+C：只退出当前本地交互 UI，run 仍保持 `WAITING_APPROVAL`；
- 自然语言如“继续”或 “go on”：不直接送入模型，因为当前还有未解决 approval。

`WAITING_TOOL_RESULT` 下，runtime 正在等待外部/client tool 结果。passive UI disconnect 或本地 Ctrl+C 只退出当前等待 UI/订阅，run 仍保持 `WAITING_TOOL_RESULT`。重新进入时恢复等待。第一版不隐式 abandon 这个 wait；如果未来需要“放弃等待 client tool result”，必须是显式控制，并写入 model-visible tool observation 后再继续。

### 5. 模型中断是明确的 inference outcome

模型请求观察到 `InterruptSignal` 后，llmd/provider 应尽快 abort 当前 request，并返回明确的 `InferenceAborted(reason=<signal.reason>)` 或等价 outcome。runtime 在 safe point 把它解释为 interrupted active work，而不是 provider failure，也不是 resumable pause。

旧 request 不 replay。后续 invocation 从 durable context 重新构建。是否写入 `ModelVisibleNotice` 由 safe point 判断，不是所有模型中断的固定副作用。

provider 网络错误、provider 自身异常、非用户来源的模型 abort 不自动归入 interrupt；它们按失败、pause 或其它 provider outcome 处理。

### 6. 工具中断是协作式协议

tool/provider 执行时必须能收到 `InterruptSignal`。runtime 不替每个工具猜测真实世界状态；tool/provider 在自己的安全点检查 signal，并报告确定 outcome：

- `succeeded`：中断到达前已经完成；
- `failed`：工具以确定错误结束；
- `interrupted`：工具确认已经协作式停止，可以给模型一个 interrupted observation；
- `unknown` / indeterminate：工具无法确认副作用是否发生，需要人工恢复。

runtime 接受工具报告并落账。只有当 runtime 失去联系、tool task 被硬取消、进程崩溃、provider 没有给出可靠 outcome 时，才使用 frozen `effect` / `risk` 做保守兜底。`effect` / `risk` 是 fallback、approval、recovery 的输入，不替代 tool/provider 的确定报告。

这意味着 `DANGEROUS` 工具不是永远不能返回 `interrupted`；只要 tool/provider 能证明自己安全停止，就可以返回 `interrupted`。反过来，任何工具只要无法确认副作用结果，都不能被 runtime 伪造成 clean interrupted。

对于非 external-write / 非 dangerous 的工具，如果 provider 因 user stop cancellation 退出且没有更精确 outcome，默认写 `interrupted` observation，而不是 `failed`。用户停止不是工具错误；只有 provider 明确报告确定错误时才写 failed。

### 7. `ModelVisibleNotice` 是可选注入，不是每次 interrupt 的副作用

Knuth 需要一种 synthetic runtime/conversation fact，用于在下一次 model call 中告诉模型某个 orchestration outcome，例如“上一段 active work 被用户中断并放弃”。这个事件不是人类用户真实输入，不能存成普通 `user.message`；但投影给推理层时必须是 `InferenceMessage.role = User`，通过普通 conversation channel 给所有 provider。

`ModelVisibleNotice` 只在 safe point 判断“下一次模型调用必须知道这件事”时写入。它不是 `InterruptSignal` 触发时的自动副作用：

- Ctrl+C 退出 approval prompt：不写 notice。
- Ctrl+C 退出 client tool result 等待 UI：不写 notice。
- tool 返回 `interrupted` 时，优先通过 canonical tool result observation 告诉模型。
- active model/tool work 被放弃，且下一次模型调用需要知道该事实时，写一次 notice。

第一版可以把 notice 的投影硬编码为 user-role inference message；但 ledger event 类型必须保留 synthetic notice 的语义，不能伪装成人类用户输入。

因为 notice 投影成 user-role message，它只能出现在 provider conversation 合法位置。若上一条 assistant turn 带 tool_use，必须先保证对应 tool observations 已写入并关闭 batch，不能把 notice 插在 assistant tool_use 和缺失 tool_result 之间。

notice policy 是：active work 被 `user_stop` 放弃后，写一条简短 notice 表达“上一轮由用户停止，不要默认重试旧动作”。active model work 被放弃时，notice 承载被丢弃的 assistant partial 事实；active tool batch 被 Ctrl+C 时，仍先写 tool observations 并关闭 batch，再在合法 conversation boundary 写 notice。waiting 状态本地退出不写 notice。

### 8. `RunSession` 拥有 live interrupt，`AgentRuntime` 不维护 session 列表

`RunSession` 是一次 `RunInvocation` 的 live handle，拥有 invocation task、live observation hub、listener queues、result awaiting 和 live interrupt handling。向正在运行的 invocation 发送 interrupt 应通过 active `RunSession` 或其 owner 完成。

`AgentRuntime` 是 durable control façade，不应该维护一张 active session registry，也不应该提供暗示“runtime 总能找到任意 live run”的 `interrupt(run_id)` 内部查找语义。

谁启动 `RunSession`，谁负责 live routing：

- CLI 只需要当前本地 foreground session；
- AG-UI / Web transport 或 host 可以维护 live manager，把 run_id 路由到 active `RunSession`；
- 未来 daemon/supervisor 可以维护自己的 live session table。

这些 manager 是 driver/host/supervisor 责任，不是 `AgentRuntime` 的 durable runtime 责任。

`RunSession.interrupt(...)` 需要通过 `InterruptController` 唤醒当前阻塞 await，但 durable 解释仍只发生在 safe point。safe point 捕获 cancellation 时必须检查 `signal.interrupted`：只有显式 signal 已触发才可解释为 interrupt 并 shield 落账；普通 session teardown 或 task-group cancellation 必须传播，不能伪造 `run.interrupted`。

`RunSession.interrupt(...) -> bool` 的返回值只表示本次调用是否首次触发 sticky signal，不保证当时仍有 active work，也不保证 durable state 已经完成 `INTERRUPTED` 转换。

### 9. Force stop 是 driver/supervisor 逃生口

第一次 Ctrl+C / UI stop 产生 graceful `InterruptSignal`。如果 tool/provider/model 没能及时协作退出，第二次 Ctrl+C 或 force stop 属于 driver/supervisor escape hatch。

Force stop 可以退出 CLI 交互、断开 UI、杀子进程或结束本地 process，但它不创建新的 run status，也不伪造 clean `tool.completed(outcome="interrupted")`。如果 durable state 没有安全写完，后续由 crash recovery、`UnknownOutcome`、`PAUSED` 或人工恢复路径处理。

graceful 阶段必须有 driver/host deadline，不能无限等待工具到达安全点。deadline 到期可以进入 force path 清理 live task 或子进程；是否能得到 clean durable outcome 仍取决于 safe point 是否已经完成。

host/live manager 对自己 deadline force-cancel 产生的遗留状态负有在线收口责任：它必须调用 runtime recovery/control primitive，把本进程失联的 active invocation 保守落成 `UNKNOWN + PAUSED`，或完成已经开始的 interrupt safe-point collapse。不能只从 live map 移除 session 并依赖 CLI-only 手动 recovery。

### 10. AG-UI/SSE 连接只是订阅，不是 run 生命周期

AG-UI transport 不应把 SSE response 生命周期等同于 `RunSession` 生命周期。SSE 是观察订阅；`RunSession` 应由 transport/host live manager 持有，直到 run 自己到达 waiting 或 terminal 状态。

分流如下：

- 浏览器刷新、网络抖动、SSE 断开：unsubscribe，不 interrupt，不 pause；
- UI stop：向 live `RunSession` 发送 `InterruptSignal`；
- 重新打开同一个 run：attach live invocation，或按 durable status 恢复 actionable state；
- 同一 run 已有 active invocation 时，新 prompt 不应悄悄开启第二个 invocation；第一版可以返回 409 或要求 attach。
- `queued_user_prompt` 这类“新输入取代当前 active turn”的 reason 只作为未来能力保留；v1 不由 CLI/AG-UI 产生。

这修订 ADR-006 中“连接断开即取消 run”的 v1 耦合模型。连接仍可作为观察流，但不能作为运行时控制事实本身。

### 11. 交互式 CLI 重入先恢复 actionable run

CLI 启动或 Ctrl+C 退出交互后重新进入时，不应总是显示空 prompt。它应先查找当前/最近的 actionable run：

- `WAITING_APPROVAL`：重弹 approval prompt；
- `WAITING_TOOL_RESULT`：恢复等待提示/订阅；
- `PAUSED`：提示可 resume；
- `RUNNING` 且有 live session：attach 观察；
- `RUNNING` 但当前进程无 live session：提示无法 attach。没有 live-run lease / heartbeat 前，不自动 recovery，因为另一个进程可能仍在执行；recovery 必须由显式命令或显式确认触发；
- `INTERRUPTED` / `SUCCEEDED`：回到普通 prompt，下一条用户输入走 `continue_run`；
- 多个 actionable runs：让用户选择，或要求显式 run id。

这样用户在 approval prompt 按 Ctrl+C 退出后，再次进入交互模式会自然回到同一个确认问题，而不是必须用外部 `approve` / `resume` 命令拼回状态。

## 后果

- 需要新增 durable `run.interrupted` 事件和 `RunStatus.INTERRUPTED`，并让 projection 明确区分 `INTERRUPTED`、`PAUSED`、`CANCELLED`。
- `tool.completed.outcome` 需要扩展 `interrupted`；工具状态机需要能表达 interrupted outcome。
- `InferenceAborted(reason=<signal.reason>)` 需要作为模型协作中断 outcome 被 runtime 明确处理。
- `RuntimeControl.resume` 必须拒绝 `INTERRUPTED`，只处理未完成控制点；`continue_run` 才是 interrupted/succeeded run 后普通用户输入的入口。
- resumable status 必须集中定义，且不能包含 `RUNNING`；`RUNNING` 是 live attach 或 explicit recovery 的对象，不是普通 resume 对象。
- 移除 `RUNNING` from resumable status 必须和 host live manager 的 attach/reentry 路由一起落地；不能让 RUNNING run 因不再 resumable 而直接掉到普通 409。
- CLI 需要统一 foreground input / interrupt driver，去掉只在部分入口存在的 Ctrl+C 处理，避免 `_read_line` 用非 daemon worker 无界等待导致退出卡住。
- AG-UI 需要把 passive disconnect 与 explicit stop 分开；`/pause` 若表达 UI stop，应迁移到 interrupt/stop 语义，而不是写 `PAUSED`。
- `RunSession` 需要提供 live interrupt 能力；AG-UI/daemon 这类多连接环境可在 host/transport 层维护 live manager，但 `AgentRuntime` 不维护 active session 列表。
- Context reconstruction 需要支持 `ModelVisibleNotice` 这类 synthetic notice：ledger 语义不是普通 user message，推理投影是 `InferenceMessage.role=User`。
- interrupt cancellation path 上的 durable ledger writes 必须 shield；否则底层取消唤醒可能吞掉本应用来收口状态的 durable facts。
- RunLedger 需要支持 `apply_many(...)` 或等价单事务 multi-draft append，用于把 tool-batch interrupt collapse 写成原子 durable fact 序列。

## 考虑过的替代方案

### 继续把 Ctrl+C 记为 `PAUSED`

拒绝。用户 stop 当前 work 的直觉是“放弃这次正在做的事”，不是“保存同一个 model request/tool batch 供稍后恢复”。把它写成 `PAUSED` 会让 `resume` 误以为可以重放旧工作，也会把 approval prompt 退出、active model 中断、client tool wait 断开混成同一类。

### 直接用 AnyIO `CancelScope` 作为所有层的取消对象

拒绝。`CancelScope` 是执行机制，不是领域协议。它不能表达来源、reason、tool/provider 协作 outcome、force stop、safe point 落账语义，也不适合持久化边界讨论。Knuth 应传递 `InterruptSignal`，底层再映射到 AnyIO 或 provider-specific abort 机制。

### 由 runtime 根据 `effect/risk` 中央判断工具中断结果

拒绝。runtime 看不到工具内部是否已安全停止、是否已经产生副作用、是否能回滚。`effect/risk` 适合 approval 和 recovery fallback，不适合压过 tool/provider 的确定报告。工具中断必须是协作式协议。

### 在 `AgentRuntime` 内维护 active session registry

拒绝。`AgentRuntime` 的职责是 durable runtime control 和 ledger-backed query；active session 发现与 routing 属于启动这些 session 的 driver/host/supervisor。把 session registry 放进 runtime 会让 CLI、AG-UI、daemon 的连接管理职责倒灌进领域 runtime。

### 把 SSE disconnect 当成 interrupt

拒绝。浏览器刷新、移动网络、标签页切换都是观察订阅生命周期变化，不是用户 stop run 的明确控制意图。显式 UI stop 才应产生 `InterruptSignal`。

### 把 interrupted notice 写成普通 `user.message`

拒绝。notice 不是用户真实输入，不能污染 durable conversation authorship。它可以在推理投影时成为 user-role `InferenceMessage`，但 ledger event 必须保留 synthetic notice 的来源语义。

### 在 `WAITING_APPROVAL` 下把下一条自然语言直接送入模型

拒绝。approval 是一个未解决的控制点。未批准/拒绝前，agent loop 不能继续，也不能把“继续”这类文本当成新的用户 prompt 绕过审批。
