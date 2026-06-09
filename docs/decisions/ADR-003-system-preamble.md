# ADR-003: SystemPreamble 装配与不持久化

## 状态
Proposed

## 日期
2026-06-09

## 背景

一个真正的 agent 的 context 不只有对话 messages。除了用户与模型的往返，还有 agent 放在对话最前面的指令：基础运行时指令、用户注入的用户级系统提示，以及未来的 skills 说明、Memory 召回事实。Knuth 此前没有任何 system prompt 支持：`ContextView` 只有 `messages` / `tools` / `diagnostics`，`reconstruct_messages_from_events` 从不插入 system message，`InferenceRole.SYSTEM` 定义了却无人使用。

现在需要增加 system prompt 支持，并且这个支持必须从一开始就为多来源、可扩展设计——而不是先塞一个 `system_prompt: str`、等 skills / Memory 到来再回头补一套装配机制。

这里的核心张力是：Knuth 以「durable run history、可恢复、可审计」立身（见 ADR-002），对话 messages 正是因此从事件日志 reconstruct。那么这块前置指令应不应该也走事件日志、被持久化？

## 决策

引入两个领域概念，并把 system prompt 建模为可扩展的多来源装配，而不是单一字符串。

### SystemPreamble

`SystemPreamble` 是运行时在每轮 build 时装配、置于 `messages` 最前的单条 system message。它是 context 的**计算投影，不是 durable 事实**：

- 每轮 `ContextBuilder.build` 重新装配，不写入 `EventStore`，不从事件日志 reconstruct。
- 一个 resume 的 run 看到的是**重新装配**的 preamble，而不是它启动时那一份的快照。如果用户级 prompt 被改过，resume 后即生效。

之所以不持久化：它的来源天然是动态的。Memory 召回逐轮变化，可用 skills 随 run 推进变化，用户级 prompt 可被编辑。把这些塞进 append-only 事件日志会让历史膨胀，并把「当前框架」误当成「发生过的事实」。这正是一个真实 agent 每轮重新拼装系统前缀的做法。

事件日志保持为纯粹的对话 / runtime 历史。可观测性层面，`model.started.message_count` 会包含 preamble；暂不引入 `has_preamble` 这类额外标记（YAGNI）。

### SystemSection

`SystemSection` 是构成 `SystemPreamble` 的可扩展片段：

```python
source: SystemSectionSource   # StrEnum: base | user | (future: skill | memory)
text: str
```

`source` 取自一个**封闭的强类型集合**，呼应 ADR-002 的强类型取向。新增来源是一次有意的类型变更（加一个枚举值 + 一类 provider），而不是靠开放字符串约定，避免拼写漂移。可扩展性来自「新增一类 provider」，不来自把 `source` 放开成 `str`。

用户注入的用户级系统提示，就是一个 `source=user` 的 `SystemSection`。

### SystemSectionProvider

`SystemSection` 由 `SystemSectionProvider` 产出：

```python
async def sections(ctx: RunContext) -> list[SystemSection]
```

它与 `MessageMiddleware` **正交**：`MessageMiddleware.process(ctx, view) -> view` 是改写整个 view 的重型、全权 seam（裁剪 / 压缩历史 / 注入诊断）；`SystemSectionProvider` 是 additive、最小权限的，只贡献片段，无权改动 messages / tools。一个**贡献**，一个**改写**。

provider 在**构造期由 agent 注入** `ContextBuilder`（与现有 `middlewares` 同一注入路径）。「用户级 prompt 从哪读」属于 agent 边缘的策略，由构造方决定（CLI flag / env / 文件均可），core 与 runtime **不预设配置格式**。现在唯一的 agent 是 knuth-cli，它在自己的 `build_runtime` 装配 `base` 与 `user` 两个 provider；runtime 的 `build_default_runtime` 只保留为 demo / 测试 helper，不承担 agent 配置策略。未来不同 agent、不同构造阶段可以分层叠加 provider。

### 装配流程

`ContextBuilder.build` 变为：

1. `reconstruct_messages_from_events` 得到对话 `messages`；
2. 聚合所有 `SystemSectionProvider` 的 `SystemSection`；
3. 按 **provider 注入顺序**（list 顺序，同 provider 内 list 顺序）用 `\n\n` 拼成一条 `InferenceMessage(role=SYSTEM)`；不引入显式 `priority` 数字，调顺序即调注入位置；core 不自动加 heading 文案；
4. 把这条 system message **prepend 到 `messages[0]`**；若没有任何非空 section，则不加（绝不发空 system message）；
5. 再跑 `MessageMiddleware`。

`SystemPreamble` 的载体就是 `messages[0]`，因此 `loop` 与 `InferenceClient.stream` **零改动**，复用已有的 `InferenceRole.SYSTEM` 映射。

## 后果

- core 新增 `SystemSection` / `SystemSectionSource`，runtime 新增 `SystemSectionProvider` 抽象与一个通用 `StaticSectionProvider` 实现，由具体 agent 的 runtime builder 实例化为 base / user / future providers。未来动态来源（如 Memory 召回）再引入各自的 provider 实现。这些抽象会被未来的 skills / Memory 依赖，契约改动成本不低。
- `ContextView` 结构不变；`messages[0]` 可能是 preamble 成为一条约定（靠 `role=SYSTEM` 判别），而非显式命名字段。
- 放弃了 run 的「精确 prompt 复现」：resume 不保证看到与启动时逐字节相同的 preamble。若将来确需精确复现，应以 diagnostic 快照实现，而不是把 preamble 重新拉回事件日志。

## 考虑过的替代方案

### 单一 `system_prompt: str` 字段

拒绝。会在 skills / Memory / 用户级 prompt 到来时被迫在一个字符串上回填多来源装配。一开始就把它建模为 `SystemSection` 列表，新增来源只是新增一个 provider。

### 把 preamble 事件化、持久化

拒绝。preamble 的来源天然动态（memory 召回、可用 skills、可编辑的用户级 prompt），事件化会让历史膨胀，并把「当前框架」误当「发生过的事实」。事件日志只保留对话与 coarse runtime 事实。

### 复用 `MessageMiddleware` 贡献 preamble

拒绝。middleware 的契约是改写整个 view，是重型、全权 seam。让它同时承担「贡献 section」会糊掉职责。贡献与改写应是两个正交 seam。

### 由 runtime 规定用户级 prompt 的配置文件契约

拒绝。「用户级 prompt 从哪来」是 agent 边缘的策略。把它固化进 runtime / core 会让 core 背上配置格式。core 只认 `SystemSectionProvider` 抽象，来源策略留在构造方（knuth-cli）。
