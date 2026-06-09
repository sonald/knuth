# ADR-004: RuntimeControl 与 AgentLoop 边界

## 状态
Proposed

## 日期
2026-06-09

## 背景

Knuth v0 的目标是让 agent loop 成为 runtime 的通用能力，而不是 knuth-cli 或未来 daemon 自己拼装的一段流程。当前实现已经把两层循环放在 `knuth-runtime`，但 public API 仍有 `run_streaming(prompt, on_event, run_id=...)` 这类参数重载入口，容易把“开始 run、继续对话、恢复等待状态、接收 live event handler”等语义混在一起。

同时，`RuntimeEvent`、live observation、hooks、approval、tool broker 都可能影响 run 的推进。若不把控制面和观察面拆开，后续 CLI、TUI、daemon、hook、工具策略都会争用同一条隐式路径。

## 决策

`AgentLoop` 是 runtime-owned orchestration cycle。knuth-cli 只构建必要配置、system section providers、event handlers、tool broker / services wiring，然后调用 runtime control surface；它不自己重建 lifecycle events，也不直接驱动 loop primitive。

Runtime builders 可以作为 public wiring helpers 保留，用于组装 storage、inference client、tool broker、policy 和 context providers；但它们不拥有 CLI 配置策略。用户级 prompt、环境变量、flags 和 agent-specific identity 的读取属于具体 agent（当前是 knuth-cli）。

public façade 类名保留 `AgentRuntime`，但它暴露的是 `RuntimeControl` 语义。`RuntimeControl` 是领域概念和方法边界，不要求立刻把类重命名。

`RuntimeControl` 是 awaited state-changing surface，应使用显式操作表达意图，而不是一个参数重载的 run API。目标入口包括：

- `start(prompt, event_handler=None)`：新建 run，记录 `run.created` / `user.message`，进入 `AgentLoop`。
- `continue_run(run_id, prompt, event_handler=None)`：已有 run 上追加普通 `user.message`，继续进入 `AgentLoop`。
- `resume(run_id, event_handler=None)`：不追加用户消息，从当前 durable state 继续推进。
- `approve(approval_id)` / `deny(approval_id)`：只解决 approval 决策，不自动 resume。
- `pause(run_id, reason)` / `cancel(run_id, reason)`：显式 runtime control 语义，分别进入 `PAUSED` / `CANCELLED`。

`RuntimeEventHandler` 只做观察型副作用，例如 CLI 渲染、日志、debug、TUI 或 WebSocket fan-out。它不通过返回值暂停、终止、审批、拒绝或改变 run 状态。状态变化必须走 `RuntimeControl` 或 awaited `BlockingHook`。

`BlockingHook` 是控制 seam，不是数据 mutation seam。v0 hooks 只允许 `continue / pause / terminate`，不允许改写 context、messages、tools、tool intent、proposal 或 inference config。需要注入 preamble 走 `SystemSectionProvider`；需要改写 context view 走 `MessageMiddleware`；需要改变工具提案和策略走 `ToolBroker` / `PolicyEngine`。

第一版 `HookPoint` 只放在状态边界或外部副作用前，不铺满所有内部步骤。`run.before_turn` 放在 context build 之前。Hook point 不自动对应 RuntimeEvent；只有 hook 导致的状态结果才形成 timeline fact。

当 blocking hook 或 runtime control 导致 pause / cancel 时，runtime 应追加 durable `run.paused` / `run.cancelled` 事件来解释 `PAUSED` / `CANCELLED` 状态变化。`CANCELLED` 表达主动终止，不复用 `FAILED`。

`ToolBroker` 是 agent loop 面向工具 workflow 的门面。agent loop 形成 `ToolIntent` 后交给 `ToolBroker.propose/execute`，不直接知道 registry、provider、policy 或 approval 的组合细节。被拒绝的工具请求应通过 canonical `tool.completed(outcome="denied")` 回到模型上下文，让模型恢复，而不是把 run 直接置为 failed/cancelled。

clarification / ask-user 类能力不属于当前 v0 边界。当前实现删除 `knuth.ask_user`、`WAITING_USER`、`user_input.requested` 和 `answered` tool outcome；以后若需要，再单独设计工具暂停、continuation、用户输入恢复等机制。

## 后果

- `run_agent_loop(...)` 可以保留为 runtime 内部 primitive，但不应是 CLI / daemon 的主要 public orchestration API。
- runtime builders 是组装 helpers，不是 agent policy owners。
- `run_streaming(prompt, on_event, run_id=...)` 是过渡 API；后续应迁移为显式 `RuntimeControl` 操作。
- 当前不立即拆分 `run_streaming`，以免和后续 live observation / runtime control 大重构互相踩踏；本 ADR 记录目标边界。
- 当前不提前加入 `run.paused` / `run.cancelled` event union；等 `pause/cancel` 或 blocking hooks 有真实生产路径时，再连同 serialization 和测试一起实现。
- approval resolution 和 run resume 是两步，调用方必须显式选择何时恢复并挂好 event handler。
- `deny` 不是 run failure；resume 时应把 denied tool request 转成 `tool.completed(outcome="denied")`，再让模型继续。
- v0 hook 设计保持窄权限，避免提前引入 patch replay、mutation persistence、冲突合并和审计复杂度。

## 考虑过的替代方案

### 继续使用一个参数重载 run API

拒绝。`prompt is None`、`run_id is None`、当前 run status 等参数组合会把 start、continue、resume、approval continuation 和 live observation 混在一个入口里，未来 daemon / UI 很难表达清楚的控制意图。

### 让 event handler 返回控制动作

拒绝。live observation 是 best-effort fan-out，不应承载状态机控制。控制必须是 awaited path，否则 backpressure、错误处理和恢复语义都会变得含糊。

### v0 hook 支持 mutate

拒绝。mutation 需要定义目标、持久化、resume replay、冲突合并和审计语义。v0 先把 hook 限制为控制决策，数据变化使用已有的 provider、middleware、broker 和 policy 边界。

### 保留 `knuth.ask_user` 作为内置控制工具

拒绝。当前边界尚未设计工具暂停和 continuation 机制，保留半套 `WAITING_USER` / `user_input.requested` 会让 agent loop 对工具名特判，并提前承诺一个未完成协议。
