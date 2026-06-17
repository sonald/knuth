# ADR-004: RuntimeControl 与 AgentLoop 边界

## 状态
Proposed

> 注：本文关于 runtime control、live observation、blocking hook 的边界仍有效；
> 其中 `MessageMiddleware` 的能力描述已由
> [MessageMiddleware 需求与设计](../message-middleware-requirements-and-design.md)
> 修订。新的 `MessageMiddleware` 是生命周期 checkpoint 上的
> `MessageTape` rewrite 组件，middleware 通过 patches / anchors 表达消息改写，
> 不通过 hook 直接 mutate context。

## 日期
2026-06-09

## 背景

Knuth v0 的目标是让 agent loop 成为 runtime 的通用能力，而不是 knuth-cli 或未来 daemon 自己拼装的一段流程。当前实现已经把两层循环放在 `knuth-runtime`，但 API 仍有 `run_streaming(prompt, on_event, run_id=...)` 和 `run_agent_loop(..., on_event=...)` 这类入口，容易把“开始 run、继续对话、恢复等待状态、接收 live event handler”等语义混在一起。

同时，`RuntimeEvent`、live observation、hooks、approval、tool broker 都可能影响 run 的推进。若不把控制面和观察面拆开，后续 CLI、TUI、daemon、hook、工具策略都会争用同一条隐式路径。

## 决策

`AgentLoop` 是 runtime-owned orchestration cycle。knuth-cli 只构建必要配置、system section providers、runtime event listeners、tool broker / services wiring，然后调用 runtime control surface；它不自己重建 lifecycle events，也不直接驱动 loop primitive。

Runtime builders 可以作为 public wiring helpers 保留，用于组装 storage、inference client、tool broker、policy 和 context providers；但它们不拥有 CLI 配置策略。用户级 prompt、环境变量、flags 和 agent-specific identity 的读取属于具体 agent（当前是 knuth-cli）。

public façade 类名保留 `AgentRuntime`，但它暴露的是 `RuntimeControl` 语义。`RuntimeControl` 是领域概念和方法边界，不要求立刻把类重命名。

`RuntimeControl` 是 state-changing surface，应使用显式操作表达意图，而不是一个参数重载的 run API。目标入口包括：

- `start(prompt, listeners=()) -> RunSession`：返回一个尚未启动的 async context manager。进入 session 时新建 run，记录 `run.created` / `user.message`，注册 listeners，并启动 `AgentLoop`。
- `continue_run(run_id, prompt, listeners=()) -> RunSession`：返回一个尚未启动的 async context manager。进入 session 时在已有 run 上追加普通 `user.message`，注册 listeners，并继续进入 `AgentLoop`。
- `resume(run_id, listeners=()) -> RunSession`：返回一个尚未启动的 async context manager。进入 session 时不追加用户消息，从当前 durable state 继续推进。
- `run_once(prompt)`：保留为 convenience API；它等价于 `async with start(prompt) as session: await session.result()`。
- `approve(approval_id)` / `deny(approval_id)`：只解决 approval 决策，不自动 resume。
- `pause(run_id, reason)` / `cancel(run_id, reason)`：显式 runtime control 语义，分别进入 `PAUSED` / `CANCELLED`。

`RunSession` 是一次 `RunInvocation` 的 temporary live handle。`start(...)` / `continue_run(...)` / `resume(...)` 调用本身不启动 agent loop；进入 `async with` 后才创建或准备 run、注册初始 listeners、发出 transient `run.invocation.started`、启动 agent loop。agent loop 完成或失败后，runtime 发出 transient `run.invocation.ended`，再关闭 listener 队列并等待短暂 drain。

`RuntimeEventListener` 是对象形态的观察者，不是裸 callback。它声明 `RuntimeEventInterest`，并异步处理匹配到的 `RuntimeEvent`。`RuntimeEventInterest` 支持精确 dotted event type、dotted type prefix、durable/transient durability 过滤；prefix matching 只是 observation-layer convenience，不把 `namespace` / `name` 重新加回 `RuntimeEvent` 模型或 `EventStore` schema。

`RuntimeEventListener` 只做观察型副作用，例如 CLI 渲染、日志、debug、metrics、TUI 或 WebSocket fan-out。它不通过返回值暂停、终止、审批、拒绝或改变 run 状态。状态变化必须走 `RuntimeControl` 或 awaited `BlockingHook`。

live observation 使用每 listener 一条 bounded AnyIO memory object stream。初始 listeners 先注册并启动 drain task，然后才发布 `run.invocation.started`。`RunSession.add_listener(listener)` 可以在 invocation 运行中添加 listener，并返回可移除的 listener handle；第一版只订阅 add 之后的新 live events，不做 durable replay 或 transient replay。

listener 默认不 required，默认 buffer size 为 100，默认 overflow policy 为 `BLOCK`。非关键 listener 可以选择 `DROP_NEWEST` 或 `DISABLE`；drop 不产生新的 runtime event，也不调用额外 callback，只记录 observation stats。listener 异常默认禁用该 listener，不影响 agent loop；required listener 失败会让 `session.result()` 抛 `RuntimeObservationError`，异常携带已经得到的 `RunResult`（如果有）。

listener queue 收尾时，`RunSession` 先关闭 send side，让 listener drain 已入队事件；默认最多等待一个短暂 grace timeout，超时后取消剩余 listener task。`session.result()` 只在 active session 中有效；context 退出后只允许读取已缓存结果或通过 runtime durable 查询获取状态和历史。

`run_agent_loop(...)` 不接受 `on_event` / `live_event_sink` 参数。`RunSession` 创建 invocation-scoped live observation hub，并把它作为 `RuntimeInvocation` 的一部分交给 agent loop。agent loop 通过 invocation 发射 runtime events；它不认识 listener、queue、renderer 或 WebSocket。

`BlockingHook` 是控制 seam，不是数据 mutation seam。v0 hooks 只允许 `continue / pause / terminate`，不允许改写 context、messages、tools、tool intent、proposal 或 inference config。需要贡献稳定 preamble fragment 走 `SystemSectionProvider`；需要在安全 checkpoint 上做消息注入、遮蔽、替换、压缩或 tool result redaction，走 `MessageMiddleware` / `MessageTape` rewrite pipeline；需要改变工具提案和策略走 `ToolBroker` / `PolicyEngine`。

第一版 `HookPoint` 只放在状态边界或外部副作用前，不铺满所有内部步骤。`run.before_turn` 放在 context build 之前。Hook point 不自动对应 RuntimeEvent；只有 hook 导致的状态结果才形成 timeline fact。

当 blocking hook 或 runtime control 导致 pause / cancel 时，runtime 应追加 durable `run.paused` / `run.cancelled` 事件来解释 `PAUSED` / `CANCELLED` 状态变化。`CANCELLED` 表达主动终止，不复用 `FAILED`。

`ToolBroker` 是 agent loop 面向工具 workflow 的门面。agent loop 形成 `ToolIntent` 后交给 `ToolBroker.propose/execute`，不直接知道 registry、provider、policy 或 approval 的组合细节。被拒绝的工具请求应通过 canonical `tool.completed(outcome="denied")` 回到模型上下文，让模型恢复，而不是把 run 直接置为 failed/cancelled。

clarification / ask-user 类能力不属于当前 v0 边界。当前实现删除 `knuth.ask_user`、`WAITING_USER`、`user_input.requested` 和 `answered` tool outcome；以后若需要，再单独设计工具暂停、continuation、用户输入恢复等机制。

## 后果

- `run_agent_loop(...)` 可以保留为 runtime 内部 primitive，但不应接收 `on_event`，也不应是 CLI / daemon 的主要 public orchestration API。
- runtime builders 是组装 helpers，不是 agent policy owners。
- `run_streaming(prompt, on_event, run_id=...)` 应删除，调用方迁移到 `start` / `continue_run` / `resume` + `RunSession` + `RuntimeEventListener`。
- `run_id` 对新 start session 来说在进入 context 后才可用；如果未来需要先创建 run 再启动 invocation，可以另行引入 `create_run + drive` 高级 API。
- `RunSession` listener scope 固定为当前 run/current invocation；all-runs observation 留给未来 runtime-level 或 daemon-level API。
- listener 实例可以复用，但 runtime 不替 listener 做 session state 隔离；有状态 renderer 应每个 `RunSession` 新建实例。
- 当前不提前加入 `run.paused` / `run.cancelled` event union；等 `pause/cancel` 或 blocking hooks 有真实生产路径时，再连同 serialization 和测试一起实现。
- approval resolution 和 run resume 是两步，调用方必须显式创建 resume `RunSession` 并挂好 listeners。
- `deny` 不是 run failure；resume 时应把 denied tool request 转成 `tool.completed(outcome="denied")`，再让模型继续。
- v0 hook 设计保持窄权限，避免提前引入 patch replay、mutation persistence、冲突合并和审计复杂度。

## 考虑过的替代方案

### 继续使用一个参数重载 run API

拒绝。`prompt is None`、`run_id is None`、当前 run status 等参数组合会把 start、continue、resume、approval continuation 和 live observation 混在一个入口里，未来 daemon / UI 很难表达清楚的控制意图。

### 让 event handler 返回控制动作

拒绝。live observation 是 best-effort fan-out，不应承载状态机控制。控制必须是 awaited path，否则 backpressure、错误处理和恢复语义都会变得含糊。

### 在 `run_streaming` 外层手写多个 callback 的 composite

拒绝。composite callback 会把 interest 声明、fan-out 顺序、异常隔离、队列背压、listener 生命周期和收尾策略都推给调用方，仍然没有把 live observation 建成 runtime 的一等边界。

### 给 listener 增加 `on_session_start` / `on_session_end` 回调

拒绝。session lifecycle callback 会形成一条不是 `RuntimeEvent` 但又像事件的旁路协议。改用 transient `run.invocation.started` / `run.invocation.ended`，让 lifecycle 也走统一的 runtime event observation 语言。

### 使用 `asyncio.Queue` 或额外队列依赖

拒绝。Knuth 当前 async 边界已经基于 AnyIO；per-listener bounded AnyIO memory object stream 能满足第一版需求，不需要把 runtime 绑死到 asyncio backend，也不需要引入外部队列库。

### v0 hook 支持 mutate

拒绝。mutation 需要定义目标、持久化、resume replay、冲突合并和审计语义。v0 先把 hook 限制为控制决策。消息层数据变化使用 `SystemSectionProvider` 或 `MessageMiddleware` 的 tape rewrite 边界；工具和策略变化使用 broker / policy 边界。

### 保留 `knuth.ask_user` 作为内置控制工具

拒绝。当前边界尚未设计工具暂停和 continuation 机制，保留半套 `WAITING_USER` / `user_input.requested` 会让 agent loop 对工具名特判，并提前承诺一个未完成协议。
