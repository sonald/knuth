# 架构变更决策：MessageMiddleware 作为 append-only projection event producer

状态：Accepted
日期：2026-06-22
相关模块：`MessageMiddleware`、`MessageTape`、`RuntimeLedger`、`ContextBuilder`、`SystemSectionProvider`、`SkillReminderMiddleware`、`TapeAnchor`

## 0. 本 ADR 的身份

本 ADR 是 `MessageMiddleware` 的目标设计和已落地迁移记录。实现、测试和相关文档已经按本文收敛；后续如果代码与本文冲突，应以本文为准并通过新的 ADR 或修订记录变更。

本 ADR 更新以下既有文档中的对应语义：

- `docs/message-middleware-requirements-and-design.md` 中关于 ephemeral injection、middleware 接收 `MessageTape`、连续 replace 限制的部分。
- `docs/skills-requirements-and-design.md` 中关于 `SkillReminderMiddleware` / `SkillChangeNoticeMiddleware` 的部分。
- ADR-003 / ADR-004 中关于 `MessageMiddleware` 能力边界的引用；但不推翻 `SystemSectionProvider` 和 `RuntimeControl` 的核心决策。

## 0.1 Implementation Status

本文目标语义已经在当前实现中落地。下面保留原迁移清单，作为实现状态和测试覆盖索引：

| 目标语义 | 实现状态 | 迁移步骤 |
|---|---|---|
| `ReplacePatch` 支持非连续 target，并把 replacement 放在最早 target 的位置 | 已实现；runner 和 ledger 均按 current projection 校验 target，不再要求连续 span | Step 5 / Step 6 |
| `MessageMiddleware.process(...)` 接收当前 model-visible `tuple[TapeMessage, ...]` | 已实现；middleware 不再接收完整 `MessageTape` | Step 2 |
| 新增通用 turn-level `AFTER_USER_MESSAGE_COMMITTED` checkpoint | 已实现；只在 `start(prompt)` / `continue_run(run_id, prompt)` 的新 user message 落盘后触发 | Step 8 |
| `MessageMiddlewareContext` 提供 `turn_start_id` 且不携带 unused budget | 已实现；`ContextBudget` 已删除 | Step 3 |
| 动态 skill catalog reminder 在每个新 turn 上判断，使用 `InsertPatch(before ctx.turn_start_id)`，并由 runner 持久化为 projection event | 已实现；`SkillReminderMiddleware` 使用 catalog digest 去重，`SkillChangeNoticeMiddleware` 已删除 | Step 8 / Step 9 |
| 删除 `TapePosition`，改用更窄的 `InsertPosition` | 已实现；尾部追加用 `InsertPatch(position=None)`，相对插入用 `InsertPosition(before/after target_id)` | Step 4 / Step 6 |
| `AGENTS.md` 不由 runtime `MessageMiddleware` 拥有 | 已实现；runtime `AgentsMDMiddleware` 已删除，CLI 通过 `SystemSectionProvider` 注入稳定 system preamble | Step 10 |
| patch class 不携带 `operation` 字段 | 已实现；`operation` 只存在于 durable rewrite anchor | Step 4 / Step 7 |
| `MessageMiddleware.name` 在 runner 内唯一 | 已实现；runner 初始化时拒绝 duplicate names | Step 11 |
| patch 原语没有 `durable` flag，所有 middleware patch 都由 runner 持久化 | 已实现；durable / ephemeral 分支已删除 | Step 11 |
| middleware runner 不返回 model-context delta | 已实现；`run_checkpoint(...)` 返回 `None`，调用方不再向 `ContextBuilder` 传入 rewrite records | Step 11 |
| middleware runner 不暴露单独 checkpoint-complete API | 已实现；public `assert_checkpoint_complete(...)` 已删除，模型请求前只调用 `BEFORE_MODEL_REQUEST` checkpoint | Step 11 |
| `ContextBuilder` 不接受 ephemeral rewrite records | 已实现；`ContextBuilder.build(...)` 只从 ledger projection 和 system section providers 构造模型输入 | Step 11 |
| patch semantic metadata 只随 synthetic `TapeMessage` 投影 | 已实现；`MessageRewriteAnchor.metadata` 已删除，semantic metadata 写入 `MessageRewriteMessageDraft.metadata` | Step 7 |
| `MessageRewriteRecord` 只保留 projection 需要字段 | 已实现；context projection record 不再携带 `rewrite_id` / `middleware` | Step 7 |

## 1. 背景

Knuth 当前使用 append-only ledger 保存运行过程中的 durable events，再从 ledger 重建 `MessageTape`，最后构造发给模型的 context。

现有设计中，`MessageMiddleware` 同时承担了几类职责：

```text
1. 压缩或替换已有 conversation messages
2. 插入 synthetic messages
3. 生成临时 ephemeral messages
4. 注入 skill reminder
5. 注入 skill change notice
6. 在 runtime middleware 中读取并插入 AGENTS.md / 项目上下文
```

这些能力都和“模型最终看到的 message sequence”有关，但它们并不都属于同一种语义。

之前版本的 ADR 曾尝试将 `MessageMiddleware` 收窄成“只做 durable rewrite”，并考虑删除 `InsertPatch`、`ephemeral` 和 `TapeAnchor`。经过进一步讨论，这个方向过度收窄了 middleware 的能力，也误伤了 skills 场景。

更准确的抽象应该是：

```text
Ledger 永远 append-only；
Middleware 不修改已有事件；
Middleware 追加 projection events；
MessageTape projector 根据 projection events 构造 model-visible context。
```

也就是说，middleware 的职责不是“原地 rewrite 消息列表”，而是：

```text
在 append-only 事件流中追加 synthetic events 和 anchor events，
让后续 projection 看起来像 insert、replace 或 append。
```

因此，本 ADR 修正以下几个点：

1. `InsertPatch` 应保留。
2. `TapeAnchor` 应保留。
3. `MessageMiddleware` 不应被命名或理解为 rewrite-only。
4. Skill reminder 不应是每次请求的 ephemeral conversation-start 注入。
5. Skill reminder 应由 middleware 策略决定是否生成。
6. 一旦 skill reminder 进入模型输入，它应是 durable synthetic user message。
7. 动态 skill catalog 应锚定在当前 turn start 前，而不是 conversation start。
8. `conversation.notice` 不适合承担需要逻辑定位的 reminder 注入。
9. 非连续 replace 应由 runner 和 ledger 共同支持。
10. Middleware 不应自己计算当前正确 message projection，runtime 应传入当前 model-visible messages。

## 2. 设计目标

本次变更遵循以下原则。

第一，保持 append-only ledger。

任何 middleware 都不能修改或删除已有 ledger events。它只能追加新的 durable events。这些新 events 可以在 model projection 中隐藏旧消息、插入 synthetic messages，或者追加 runtime-visible messages。

第二，cache-first。

Harness 的第一优先级之一是 prompt cache 友好。任何会频繁变化的动态状态，都不应该反复插入 stable prefix 的开头。动态状态应尽量锚定在最新 turn 的开始位置，避免让旧 turns 的 token prefix 失效。

第三，runtime 拥有 projection。

Middleware 不应该自己调用 `tape.model_visible()`、过滤 `origin`、解释 `TapeAnchor`、或者推断当前哪些消息已经被 rewrite 隐藏。Runtime 应将当前 model-visible messages 作为只读快照传给 middleware。

第四，保留简单原语。

不引入复杂 patch algebra，不引入 `messages_by_id + visible_order + ledger_order` 这类额外实体。继续使用当前核心原语：

```text
MessageTape
TapeMessage
TapeAnchor
InsertPatch
InsertPosition
ReplacePatch
MessageRewriteAnchor
MessageRewriteMessage
```

第五，区分 static protocol 和 dynamic state。

稳定协议可以放入 system preamble；动态状态应作为 timeline 中的状态变化进入 conversation projection。

## 3. 核心模型

### 3.1 Ledger 物理顺序永远 append-only

运行时只能向 ledger 尾部追加事件：

```text
seq 1: user.message
seq 2: model.completed
seq 3: tool.invocation_completed
seq 4: message.rewrite_anchor
seq 5: message.rewrite_message
seq 6: message.rewrite_anchor
```

任何 middleware patch 都会被编译成 append-only events。

因此，middleware 不是在 ledger 中“移动消息”“删除消息”或“覆盖消息”。它只是追加一段描述 projection 行为的事件。

### 3.2 Projection 可以呈现逻辑 insert / replace / append

物理事件顺序和模型看到的逻辑消息顺序不同。

例如，当前已有：

```text
m1: user message
m2: assistant message
m3: user message
```

之后 middleware 追加一个 insert patch：

```text
rw1 begin: operation=insert, position=before m3
rw1#0: synthetic user message
rw1 end
```

物理 ledger 是 append-only：

```text
m1
m2
m3
rw1 begin
rw1#0
rw1 end
```

但 projection 可以得到：

```text
m1
m2
rw1#0
m3
```

同理，replace patch 也不是删除原消息，而是追加：

```text
rw2 begin: operation=replace, suppresses=[m1, m3]
rw2#0: synthetic summary message
rw2 end
```

projection 中：

```text
m1 和 m3 被 suppress
rw2#0 出现在最早 target 的位置
```

所以最终模型看到的是 projection，不是 ledger 物理顺序。

### 3.3 `TapeAnchor` 保留

`TapeAnchor` 是 projection marker，不是 conversation message。

它负责表达：

```text
某个 rewrite 在 model-visible projection 中 suppress 哪些 messages。
```

`MessageTape.items` 继续可以包含：

```python
TapeItem = TapeMessage | TapeAnchor
```

不要把 `MessageTape.items` 收窄成纯 `list[TapeMessage]`。这样会破坏当前 raw/model projection 分流。

保留两个视图：

```text
raw_ledger_messages()
    返回原始 durable ledger messages

model_visible()
    根据 TapeAnchor.suppresses 隐藏被 replace 的 messages
    返回模型当前应该看到的 TapeMessage
```

`TapeMessage.origin` 保留。它不参与 patch target 合法性；target 合法性只看 current model-visible projection。但 `origin` 仍有真实用途：`raw_ledger_messages()`、middleware 自身策略（例如 compaction 不压缩已有 summary）、debug 和 audit 都需要区分 ledger 原始消息与 middleware synthetic 消息。

因此，本 ADR 不删除 `TapeAnchor`，也不推翻已有 tape identity / projection 重构。

若未来要删除 `TapeAnchor`，必须单独写 ADR，明确替代这些能力：

```text
raw audit read API
model projection API
rewrite lineage API
historical replay compatibility
suppression tracking
```

本次变更不处理这些内容。

## 4. Patch 原语

### 4.1 `InsertPatch` 保留

`InsertPatch` 是必要原语。

它表达：

```text
在 model-visible projection 的某个逻辑位置显示一组 synthetic messages。
```

它不等于“破坏 append-only ledger”。它的实现方式仍然是向 ledger 尾部追加 anchor/message events。

`InsertPatch` 可以表达三种常见情况：

```text
before target
    在某个可见 message 前插入 synthetic messages

after target
    在某个可见 message 后插入 synthetic messages

position=None
    在当前 conversation projection 尾部追加 synthetic messages
```

因此不需要新增 `AppendPatch`。逻辑 append 是 `InsertPatch(position=None)` 的特例。

删除 `TapePosition`，改成只服务 insert 的窄类型：

```python
class InsertPosition(KnuthModel):
    kind: Literal["before", "after"]
    target_id: str
```

`InsertPatch.position` 类型为 `InsertPosition | None`。`InsertPosition` 只表达相对某条可见 message 的 `before` / `after`；尾部追加用 `position=None` 表达。

`InsertPosition.target_id` 可以指向 ledger 原始 message，也可以指向之前 middleware 生成的 synthetic model-visible message。合法性只看当前 model-visible projection，不按 `origin` 排除 synthetic messages；已经被 suppress 的 message 和同一批 patches 新生成的 message 都不能作为 insert target。

`conversation_start` 不再是 middleware projection boundary：稳定开头上下文走 `SystemSectionProvider`，动态 turn-aware 状态用 `before target` 锚定到具体 message。

`before_model_request` 是 `MessageMiddlewareCheckpoint`，不是 projection boundary；如果 middleware 需要在模型请求前追加可见 synthetic message，应在 `BEFORE_MODEL_REQUEST` checkpoint 返回 `InsertPatch(position=None)`。

`InsertPatch` 的 items 必须非空。

`InsertPatch` 不包含 `durable` flag。目标态中，middleware patch 一律由 runner 编译成 durable projection events；不落盘的稳定上下文应走 `SystemSectionProvider`，不是 `InsertPatch`。

### 4.2 `ReplacePatch` 支持非连续 target

`ReplacePatch` 表达：

```text
隐藏 target_ids 对应的当前可见 messages；
在这些 targets 中最早出现的位置显示 replacement_items。
```

例如：

```text
原始 projection:
m1, m2, m3, m4, m5

ReplacePatch:
target_ids = [m1, m3, m5]
replacement_items = [s1]

结果 projection:
s1, m2, m4
```

这里不要求 `m1, m3, m5` 连续。

`target_ids` 的输入顺序没有语义。Runtime 应按当前 projection 顺序确定最早 target 的位置。

`ReplacePatch` 的约束：

```text
target_ids 非空
target_ids 内不能重复
所有 target 必须存在于当前 model-visible projection
replacement_items 非空
```

`target_ids` 可以指向 ledger 原始 messages，也可以指向之前 middleware 生成的 synthetic model-visible messages。合法性只看当前 model-visible projection，不按 `origin` 排除 synthetic messages。

本 ADR 不支持纯删除 rewrite，也就是不支持：

```text
1 -> 0
N -> 0
```

如果未来需要纯删除，应单独扩展，并明确 tool-call/tool-result 结构、audit 占位、以及 replay 语义。

`ReplacePatch` 同样不包含 `durable` flag。所有 `ReplacePatch` 都由 runner 编译成 durable projection events。

### 4.3 Replacement 固定放在最早 target 位置

本 ADR 不引入 `anchor_id`。

对于非连续 targets，replacement 的位置固定为：

```text
所有 target 中，在当前 projection 中最早出现的 target 位置。
```

这让 patch 结构保持最小：

```python
class ReplacePatch(KnuthModel):
    target_ids: list[str]
    replacement_items: list[InferenceMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)
```

`InsertPatch` / `ReplacePatch` patch class 不包含 `operation` 字段。Patch 类型由 concrete class 表达；runner 编译出的 durable `MessageRewriteAnchor.operation` 仍保留，用于 replay、ledger validation 和 audit。

如果未来确实需要 middleware 精确控制 replacement 落点，再考虑增加 `anchor_id` 或新的 patch 类型。

## 5. MessageMiddleware 的新定义

### 5.1 Middleware 不是 rewrite-only

`MessageMiddleware` 不应被理解为“重写消息列表的 hook”。

它更准确的定义是：

```text
MessageMiddleware 是 append-only ledger 上的 projection event producer。
```

它接收当前 model-visible messages 的快照，返回一组 projection patches。

这些 patches 最终会被 runner 编译成 append-only events：

```text
MessageRewriteAnchor
MessageRewriteMessage
MessageRewriteAnchor
```

业务层和 middleware 不应直接构造 `MessageRewriteAnchorDraft` / `MessageRewriteMessageDraft`。这些 rewrite drafts 是 runner 编译 patches 后写入 ledger 的内部事件形态；ledger 级测试或低层迁移可以把它们当 escape hatch 使用，但正常扩展点是 `InsertPatch` / `ReplacePatch`。

projection 阶段再根据这些 events 构造最终 context。

因此，`MessageMiddleware` 可以做：

```text
insert synthetic user message
replace old messages with summary
append synthetic runtime message
condense tool result
inject skill reminder before current turn
```

但它不能直接修改 ledger 已有事件。

### 5.2 Middleware 不接收 MessageTape

当前接口把整个 `MessageTape` 传给 middleware，导致每个 middleware 都要自己判断：

```text
应该调用 model_visible() 吗？
是否过滤 origin == MIDDLEWARE？
是否包含之前压缩生成的 summary？
是否应该看到 TapeAnchor？
```

这是错误的职责划分。

新的接口应改为：

```python
class MessageMiddlewareContext(KnuthModel):
    run_id: str
    checkpoint: MessageMiddlewareCheckpoint

    # 当前 turn 起始 user message 的稳定 message id。
    # 用于 cache-aware insert，例如 skill reminder 插在当前 turn 前。
    turn_start_id: str | None = None


class MessageMiddleware(ABC):
    name: str
    priority: int = 100
    checkpoints: set[MessageMiddlewareCheckpoint]

    @abstractmethod
    async def process(
        self,
        ctx: MessageMiddlewareContext,
        messages: tuple[TapeMessage, ...],
    ) -> list[MessageTapePatch]:
        ...
```

其中 `messages` 是 runtime 已经投影好的当前 model-visible messages。

Middleware 可以根据这些 messages 选择 target、判断是否注入、生成 replacement，但不能再自己重建 projection。

### 5.3 Runner 负责 projection 和顺序组合

Runner 的处理流程应是：

```python
events = await ledger.list_events(run_id)
tape = await reconstruct_message_tape_from_events(events)

for middleware in candidates:
    messages = tuple(tape.model_visible())

    patches = await middleware.process(ctx, messages)

    validate_patch_plan(messages, patches)

    if patches:
        drafts = compile_patches_to_append_only_events(middleware, patches)
        await ledger.apply_many(run_id, drafts)

        events = await ledger.list_events(run_id)
        tape = await reconstruct_message_tape_from_events(events)
```

这保证：

```text
M1 输入 V0，生成 patches P1
P1 写入 ledger
重建 projection 得到 V1

M2 输入 V1，生成 patches P2
P2 写入 ledger
重建 projection 得到 V2
```

后面的 middleware 必须基于前面 middleware 已经生效后的 context 工作。

例如：

```text
原始:
m1, m2, m3, m4

M1:
[m1, m3] -> s1

M2 收到:
s1, m2, m4

M2:
[s1, m2] -> s2

最终:
s2, m4
```

因此，多次压缩自然基于模型实际看到的 context，而不是基于原始 ledger messages。

middleware 顺序继续由 `priority` 决定，数值小的先运行。同一个 `priority` 下按注册顺序稳定排序；排序键是 `(priority, registration_order)`。`priority` 只提供确定顺序，不是依赖图；第一版不设计 middleware dependency graph。

`MessageMiddleware.name` 必须在同一个 runner 内唯一。Runner 初始化时发现重复 name 应直接报错；不引入命名空间、自动改名或注册表。该约束只保证 durable `MessageRewriteAnchor.middleware` 和 `rewrite_audit()` 中的 middleware 字段能定位到唯一 middleware。

### 5.4 `BEFORE_MODEL_REQUEST` 的写入边界

所有 middleware patch 都是 durable，并不意味着 `ContextBuilder.build()` 可以写 ledger。`BEFORE_MODEL_REQUEST` checkpoint 可以写入 projection events，但只能发生在 live `RunInvocation` 的同步 critical path 中：

```text
AgentLoop / RunInvocation
    -> run_checkpoint(BEFORE_MODEL_REQUEST)
    -> 如果写入 projection events，重新读取 ledger / 重建 projection
    -> ContextBuilder.build() 纯读构造模型输入
    -> step.started / model request
```

`BEFORE_MODEL_REQUEST` 写入必须发生在 `step.started` 之前。`step.started` 上的 `ContextSnapshot.messages_hash` 必须基于写入后、最终要发给模型的 messages 计算；否则 snapshot 只能证明旧上下文，不能解释模型实际看到的输入。

checkpoint 失败语义按生命周期分级，不让 runner 混入 middleware 策略：

```text
middleware 内部策略
    如果某个错误可接受，middleware 自己处理并返回空 patches。
    middleware 不应通过异常表达“我决定跳过”；跳过是正常策略结果，必须返回空 patches。
    如果异常逃出 process(...)，runner 只把它视为该 checkpoint 执行失败，不判断它是 required 还是 optional。
```

```text
BEFORE_MODEL_REQUEST
    模型请求马上要发出；如果 checkpoint 执行失败，本次 model request 不能继续使用旧 projection。
    如果失败可重试或需要人工处理，run 进入 PAUSED；不可恢复才进入 FAILED。

AFTER_USER_MESSAGE_COMMITTED
    新 turn 的通用策略 checkpoint；失败不阻塞用户 turn，也不让 run 进入 PAUSED / FAILED。
    如果 middleware 已经产生 patch，则 patch 持久化必须原子；写入失败时记录非 durable log / debug stats，本 turn 可继续。
    任何真正会影响模型请求是否可发出的条件，都应在 BEFORE_MODEL_REQUEST 重新检查。

AFTER_TOOL_RESULT_COMMITTED
    工具结果提交后的整理 checkpoint；失败不直接 fail run。
    如果整理结果影响模型请求是否可发出，例如 observation 必须 condensation，下一次 BEFORE_MODEL_REQUEST 负责重新检查。

AFTER_TURN_CLOSED
    维护型 checkpoint。失败不应推翻已经完成的回答，也不应把 SUCCEEDED run 直接改成 FAILED。
    如果 compaction 仍然必要，记录非 durable log / debug stats，并让下一次 BEFORE_MODEL_REQUEST 兜底补跑。
```

`ContextCompactionMiddleware` 保留 `AFTER_TURN_CLOSED` + `BEFORE_MODEL_REQUEST` 双 checkpoint。`AFTER_TURN_CLOSED` 是优先维护路径；`BEFORE_MODEL_REQUEST` 是 crash/missed checkpoint 后的模型请求前兜底，因为 context size 会影响模型请求是否能安全发出。这一点不同于 skill catalog reminder，后者不属于模型请求前置条件。

通用 patch 机制允许 target 当前可见的 synthetic middleware messages，但 `ContextCompactionMiddleware` 可以保留自己的保守策略：只压缩 ledger-origin messages，不压缩已有 summary，避免 summary-of-summary 质量下降。这是 middleware 策略，不是 ledger/runner 的通用限制。

`ObservationCondensationMiddleware` 保留 `AFTER_TOOL_RESULT_COMMITTED` + `BEFORE_MODEL_REQUEST` 双 checkpoint。`AFTER_TOOL_RESULT_COMMITTED` 是优先整理路径；`BEFORE_MODEL_REQUEST` 是请求前兜底，因为超长 tool result 是否已经 condensation 会影响模型请求是否能安全发出。目标态不保留 public `assert_checkpoint_complete()`；该完整性检查并入 `run_checkpoint(BEFORE_MODEL_REQUEST)` 的内部流程或 runner 内部 helper。

本 ADR 暂不新增 middleware failure durable event。只有当维护型 checkpoint failure 需要成为用户可见恢复流程，或必须跨进程追踪失败原因时，再单独设计事件协议。

这些路径不能触发 middleware：

```text
ContextBuilder.build()
AgentRuntime.model_context_messages()
raw history / UI history query
rewrite audit read API
任何 read-only projection
```

read API 只读取已经持久化的事实；它们不能因为“马上要展示/调试模型上下文”而生成新的 projection events。

### 5.5 同一个 middleware 返回多个 patches

同一个 middleware 可以返回多个 patches。

例如 observation condensation 可以一次替换多个 tool results：

```text
t1 -> condensed_t1
t4 -> condensed_t4
t7 -> condensed_t7
```

这些 patches 基于同一份输入 messages 快照。

同一批 patches 的规则：

```text
每个 patch 独立
target sets 不得重叠
不能依赖同批其他 patch 生成的 replacement
runtime 原子验证
runtime 原子提交
```

也就是说，同一个 middleware 返回的 patches 不能表达：

```text
P1 生成 s1
P2 立刻 target s1
```

如果需要这种依赖，应该拆成两个 middleware，或者让同一个 middleware 内部计算最终结果后只返回一个 patch。

同一个 middleware 的一次 `process(...)` 返回值是一个原子 patch plan。runner 必须先基于同一份输入 projection 整体验证这批 patches，再一次性写入 ledger；如果其中任何一个 patch 校验失败，整批失败且不能写入任何 patch。runner 不能写入前几个 patch 后再发现后面的 patch 失败。

不同 middleware 之间不做跨 middleware 回滚。M1 的 patch plan 一旦成功写入，就成为 durable fact；M2 基于 M1 生效后的 projection 运行。若 M2 失败，只影响 M2 及后续 middleware，不回滚 M1。

某个 middleware 失败后，runner 停止当前 checkpoint，不继续运行后续 middleware。是否允许跳过失败条件是该 middleware 自己的策略；若它要跳过，应捕获错误并返回空 patches。

## 6. Ledger 事务校验

非连续 rewrite 不能只修改 runner。`RunLedger.apply_many` 是 durable 状态的最终守门人，也必须支持同样的语义。

### 6.1 Replace transaction invariants

对于一次事务中的 replace patches，应校验：

```text
target_ids 非空
target_ids 内无重复
target_ids 全部存在于事务开始时的 current projection
target_ids 不要求连续
target 可以是 ledger message，也可以是已经存在于 current projection 中的 synthetic middleware message
同批 replace patches 的 target sets 不得重叠
replacement_items 非空
不能 target 同一事务内其他 patch 新生成的 replacement message
```

事务内的 patch 都基于事务开始时的 projection。

例如事务开始时：

```text
m1, m2, m3, m4, m5
```

同一批 patches：

```text
P1: [m1, m3] -> [s1]
P2: [m4]     -> [s2]
```

合法，结果为：

```text
s1, m2, s2, m5
```

但：

```text
P1: [m1, m3] -> [s1]
P2: [m3]     -> [s2]
```

非法，因为 target overlap。

### 6.2 Insert transaction invariants

对于 insert patches，应校验：

```text
items 非空
position 为 None 表示 append 到当前 projection 尾部
如果 position 是 InsertPosition，则 target_id 必须存在于事务开始时的 current projection；target 可以是 ledger message，也可以是已经存在于 current projection 中的 synthetic middleware message
```

如果同一批 patches 都插在同一个位置，顺序按 middleware 返回顺序和 patch ordinal 决定。

### 6.3 Projection replay 必须确定

`_apply_rewrite_records` 应按 ledger 中 rewrite records 的顺序依次应用。

伪代码：

```python
items = list(base_items)

for record in rewrite_records:
    if record.operation == "replace":
        apply_replace_projection(items, record)
    elif record.operation == "insert":
        apply_insert_projection(items, record)

return items
```

Replace projection：

```python
def apply_replace_projection(items, record):
    suppresses = set(record.suppresses)

    target_indexes = [
        idx
        for idx, item in enumerate(items)
        if isinstance(item, TapeMessage) and item.id in suppresses
    ]

    if not target_indexes:
        return

    insert_at = min(target_indexes)

    items[insert_at:insert_at] = record.messages
```

这里 `record.messages` 应包含用于 suppress 的 `TapeAnchor` 和 replacement `TapeMessage`。

Insert projection：

```python
def apply_insert_projection(items, record):
    if record.position is None:
        insert_at = len(items)
    else:
        insert_at = insert_position_index(items, record.position)
    items[insert_at:insert_at] = record.messages
```

Ledger 写入时应保证 target/position 合法；projector 不应承担复杂修复。

## 7. Skill catalog 的新语义

### 7.1 Skill reminder 不是当前请求能力快照

旧设计把 `SkillReminderMiddleware` 做成：

```text
每次 BEFORE_MODEL_REQUEST 读取当前 skill catalog
生成 ephemeral user message
插入 conversation_start
```

这个语义不对。

Skill catalog 不是“每次请求重新注入的能力快照”。它是随时间变化的外部能力状态。

模型需要知道的是：

```text
从某个时间点开始，当前 run 可见的 skill catalog 发生了变化。
```

因此，skill reminder 应作为 timeline 中的 synthetic user message，在合适的 turn 边界进入 context。

### 7.2 Skill reminder 应由 middleware 决策

保留 skill middleware 是合理的，因为“是否提醒”是策略。

Middleware 可以判断：

```text
catalog digest 是否变化
变化的 skills 是否和当前 turn 有关
本 turn 或最近几个 turns 是否引用过相关 skill
当前 turn 是否已经注入过相同 digest 的 reminder
插入 reminder 的 cache 代价是否值得
```

因此，skill reminder 不应该退化成无条件 provider，也不应该被简单替换成 `conversation.notice`。

### 7.3 Skill reminder 应插在当前 turn start 前

动态 skill reminder 不应插在 conversation start。

正确位置是：

```text
当前 turn 的 user message 之前
```

例如当前 projection：

```text
system preamble
turn 1
turn 2
turn 3
turn 4 user message
```

如果在 turn 4 需要注入 skill reminder，应得到：

```text
system preamble
turn 1
turn 2
turn 3
skill reminder
turn 4 user message
```

这样 prefix cache 可以命中到 turn 3。

如果插在 conversation start：

```text
system preamble
skill reminder
turn 1
turn 2
turn 3
turn 4 user message
```

一旦 reminder 内容变化，turn 1 之后的全部 prefix 都会失效。

因此，对于动态状态，cache-friendly 的定位规则是：

```text
尽量锚定在最新需要感知该状态变化的 turn start 前。
```

### 7.4 Skill reminder 一旦注入，应 durable

如果 reminder 进入了模型输入，并可能影响模型行为，它就应该是 durable。

否则会出现：

```text
模型实际看到了 reminder
但 replay 无法从 ledger 重建同样的 context
```

因此新设计中：

```text
SkillReminderMiddleware 可以决定跳过；
但一旦返回 InsertPatch，runner 必须把它持久化为 projection events。
```

不再使用 ephemeral skill reminder。

### 7.5 Skill reminder 在 turn 边界运行

Skill catalog reminder 不应跑在每次 `BEFORE_MODEL_REQUEST` 上。一个 turn 里可能有多次模型请求，例如模型请求工具、工具返回、模型再请求；这些请求仍属于同一个用户 turn，不应该重复判断或插入 catalog reminder。

目标 checkpoint 是通用 runtime lifecycle checkpoint：

```text
AFTER_USER_MESSAGE_COMMITTED
```

这个 checkpoint 不属于特定 middleware。它只在 host / runtime 为新 turn 追加 `user.message` 后运行：

```text
start(prompt)
    user.message committed
    AFTER_USER_MESSAGE_COMMITTED
    first BEFORE_MODEL_REQUEST

continue_run(run_id, prompt)
    user.message committed
    AFTER_USER_MESSAGE_COMMITTED
    first BEFORE_MODEL_REQUEST
```

`resume(run_id)` 不追加新的用户消息，因此不触发 skill reminder 的 turn-level 判断。它继续推进已有等待点，不应凭空把新的 skill catalog reminder 插到已经开始的 turn 前面。

`AFTER_USER_MESSAGE_COMMITTED` 只允许在追加新 `user.message` 的路径触发，例如 `start(prompt)` 和 `continue_run(run_id, prompt)`。这些路径开启新 turn。`resume`、approval resolve、tool result submit、crash recovery 都是在推进已有 turn，不能触发该 checkpoint。

Skill catalog reminder 是 best-effort but durable-if-written：catalog 判断失败、读取失败或 patch 写入失败不阻塞本 turn；但如果成功写入，后续 replay 必须能从 ledger 重建同样的 context。

`BEFORE_MODEL_REQUEST` 不检查 skill reminder 是否存在。它只检查 provider-valid message sequence、必须完成的 observation condensation、以及其它模型请求前不可缺少的结构性条件；skill catalog reminder 缺失不属于模型请求前置条件。

skill reminder 不做同一 turn 内的自动重试，也不引入额外 retry 状态。下一次相关 checkpoint 自然触发时重新判断 catalog digest 和注入策略即可。

### 7.6 Skill reminder 使用 InsertPatch

Skill middleware 返回：

```python
InsertPatch(
    position=InsertPosition(
        kind="before",
        target_id=ctx.turn_start_id,
    ),
    items=[
        InferenceMessage(
            role=InferenceRole.USER,
            content=render_skills_reminder_text(snapshot),
        )
    ],
    metadata={
        "category": "skill_reminder",
        "catalog_digest": snapshot.catalog_digest,
        "snapshot_version": snapshot.version,
        "reason": "catalog_changed",
    },
)
```

`AFTER_USER_MESSAGE_COMMITTED` 必须提供 `ctx.turn_start_id`。如果没有 `turn_start_id`，skill reminder middleware 应跳过或报错，不退化到 append / `position=None`；否则会把动态 catalog 状态放到错误的 turn 边界。

### 7.7 删除 SkillChangeNoticeMiddleware

旧设计中存在两个 middleware：

```text
SkillReminderMiddleware
    BEFORE_MODEL_REQUEST
    ephemeral
    conversation_start

SkillChangeNoticeMiddleware
    AFTER_TURN_CLOSED
    durable
    conversation_end
```

新设计应收敛成一个更清晰的 middleware：

```text
SkillReminderMiddleware
    AFTER_USER_MESSAGE_COMMITTED
    InsertPatch persisted by runner
    before current turn start
    policy decides whether to skip
```

保留类名 `SkillReminderMiddleware`，但语义应改为：

```text
turn-level skill catalog reminder middleware
```

删除 `SkillChangeNoticeMiddleware` class、exports、builder wiring 和相关测试引用，不保留 deprecated alias。保留 alias 会继续暗示 turn-end `conversation_end` notice 是可用路径。

而不是：

```text
per-request ephemeral reminder
```

### 7.7 SkillSystemSectionProvider 只放静态协议

skills 可拆成两部分：

```text
静态协议
    如何理解 skill
    如何请求 skill
    skill catalog reminder 的格式说明
    放入 SystemSectionProvider

动态 catalog
    当前有哪些 skills
    catalog digest/version
    由 middleware 在 turn boundary 注入
```

也就是说：

```text
SkillSystemSectionProvider
    保留，但只放稳定规则

SkillReminderMiddleware
    注入动态 catalog 状态
```

不要把动态 catalog 放到每次请求的 system preamble 中。

## 8. `conversation.notice` 的职责边界

`conversation.notice` 当前会在 ledger 顺序位置投影成 user-role message。

它适合表达自然发生在 timeline 尾部的 runtime 事实，例如：

```text
run interrupted
run resumed
runtime warning
verification feedback
```

它不适合表达需要逻辑定位的 message injection。

Skill reminder 需要：

```text
由 middleware 策略决定是否注入
锚定到当前 turn start 前
携带 catalog metadata
参与 projection
保持 cache-friendly
```

因此 skill reminder 应使用 `InsertPatch`，而不是 `conversation.notice`。

`conversation.notice` 可以继续保留给不需要定位的 runtime facts。

## 9. Metadata 投影

Middleware 后续判断不能依赖解析 message content。

例如旧 skills 设计通过正则从 reminder 文本中提取 catalog digest。这不稳定。

新的 contract 应要求：

```text
patch.metadata 中的 semantic metadata 必须随 synthetic TapeMessage 一起投影出来。
```

编译 patch 时，semantic metadata 应同时进入 `MessageRewriteMessageDraft`：

```python
metadata = _semantic_metadata(patch.metadata)

messages = [
    MessageRewriteMessageDraft(
        message=item,
        metadata=metadata,
    )
    for item in patch.items
]
```

Metadata 是 patch-level，不支持 per-message metadata。一个 patch 生成多条 synthetic messages 时，这些 messages 得到同一份 semantic metadata。如果不同 synthetic messages 需要不同语义，应拆成多个 patches，而不是引入 `PatchItem` 或在 patch 内嵌 per-message metadata。

`MessageRewriteMessageDraft` 不携带 `message_id`。Stored `MessageRewriteMessage.message_id` 由 ledger 在写入 rewrite block 时按 `rewrite_id + ordinal` 生成；middleware patch 和 draft 调用方不能控制 synthetic message id。

不要把同一份 semantic metadata 再复制到 `MessageRewriteAnchor`。目标态删除 `MessageRewriteAnchor.metadata` 字段；anchor 只保留 projection 所需的 runtime 字段，middleware 后续判断应读取 synthetic `TapeMessage.metadata.semantic`。

这不删除 `MessageRewriteAnchor.kind = begin/end`。Begin/end 是 durable rewrite block framing：projection fold、ledger validation 和 audit 都需要知道一个 rewrite block 何时打开、何时完整关闭。

因此 `rewrite_audit()` 也不再暴露 anchor-level `metadata`。Audit 只保留 rewrite identity、middleware、operation、position、suppresses、begin/end seq，以及 replacement messages 上的 metadata。

ReplacePatch 同理：

```python
messages = [
    MessageRewriteMessageDraft(
        message=item,
        metadata=metadata,
    )
    for item in patch.replacement_items
]
```

Projection 后，middleware 可以读取：

```python
semantic = item.metadata.get("semantic", {})

if semantic.get("category") == "skill_reminder":
    digest = semantic.get("catalog_digest")
```

这样可以可靠识别：

```text
某条 synthetic message 来自哪个 middleware
它表达哪类语义
它对应哪个 catalog digest
是否已经 supersede
```

Metadata 不应使用 runtime reserved keys，例如：

```text
rewrite_id
message_id
origin
visibility
middleware
operation
position
kind
```

这些由 runtime 负责。

## 10. AGENTS.md / project instructions

`AGENTS.md` 不应由 runtime `MessageMiddleware` 拥有。它是 host / agent 边缘的项目指令读取策略；当前 host 是 `knuth-cli`，因此 `AGENTS.md` 支持应属于 `knuth-cli` 的 project instruction 功能。

runtime 只保留通用抽象：

```text
SystemSectionProvider
    接收 host 已经决定好的 stable project instruction fragment

MessageMiddleware
    不读取 AGENTS.md
    不扫描项目文件
    不实现 project instruction hot reload
```

如果 `knuth-cli` 选择支持 run-frozen `AGENTS.md`，它可以在 run 创建 / runtime 构造时读取一次，并通过 `SystemSectionProvider` 放入稳定 system preamble。这样它不会在 run 中频繁变化，也不会破坏 cache。

如果未来要支持 hot reload `AGENTS.md`，那是 `knuth-cli` / host 的新 project instruction feature，应另行设计它如何在 safe point 上通知模型。这个能力不应通过 runtime 内置 `AgentsMDMiddleware` 隐式获得。

因此，本 ADR 的目标不是把 `AGENTS.md` 迁移成另一种 runtime middleware，而是删除 `AgentsMDMiddleware` 这类 runtime-owned 项目文件读取逻辑。

目标边界：

```text
knuth-cli / host
    发现、读取、缓存或热更新 AGENTS.md
    决定哪些 project instructions 进入当前 run

runtime
    接收 SystemSectionProvider 贡献的 stable preamble fragment
    不知道 AGENTS.md 文件路径或加载规则
```

## 11. ContextBuilder 的职责

`ContextBuilder` 负责：

```text
1. 从 ledger 重建 MessageTape
2. 应用 ledger 中已经持久化的 projection events
3. 获取 model-visible messages
4. 拼接 stable system preamble
5. 运行 redactor / tool filter
6. freeze ContextSnapshot
```

它不负责执行 middleware 策略。

Middleware 应在 `ContextBuilder.build()` 前运行，并将 patches durable 写入 ledger。`ContextBuilder.build()` 不接收调用方提供的临时 rewrite records。

构建顺序：

```text
ledger events
    -> MessageTape projection with durable projection events
    -> model-visible conversation messages

system section providers
    -> stable system preamble

final model input
    -> system preamble + model-visible conversation messages
```

注意：

```text
MessageMiddleware 不处理 system preamble
MessageMiddleware 不能 target system section
MessageMiddleware 只处理 durable conversation projection
```

如果动态 skill 状态需要进入 conversation，它应通过 `InsertPatch` 成为 synthetic user message，而不是修改 system preamble。动态 project instruction / `AGENTS.md` hot reload 不属于 runtime `MessageMiddleware` 的目标范围。

## 12. Cache-first placement policy

Harness 应遵循以下 cache policy：

```text
稳定内容放在前面
变化内容放在靠近变化发生的位置
不要在 conversation_start 注入频繁变化的动态状态
不要每次请求改写 system preamble
不要为了提醒模型而让旧 turns 全部失去 prefix cache
```

典型分类：

```text
稳定 base instructions
    system preamble

稳定 skill 使用协议
    system preamble

run-frozen project instructions
    system preamble

动态 skill catalog
    InsertPatch before current turn_start_id, persisted by runner

context compaction summary
    ReplacePatch at earliest target position

tool observation condensation
    ReplacePatch at original tool result position
```

对于当前 turn 内发生的动态变化：

```text
如果变化与当前 turn 无关
    middleware 可以跳过

如果变化与当前 turn 后续模型请求有关
    插入 before current turn_start_id
    只使当前 turn 之后 cache 失效

如果变化影响工具可用性或安全性
    必须注入，必要时阻止模型请求直到状态一致
```

## 13. Provider message validation

每次 middleware patches 生成后，runner 应验证候选 projection 是否仍然满足 provider message 结构。

尤其是：

```text
system message 必须只出现在开头
assistant tool calls 必须紧跟 tool results
不能产生 dangling tool result
不能删除 tool call 却留下 tool result
```

由于本 ADR 不支持空 replacement，纯删除导致的结构破坏风险暂时不进入范围。

但 replace 仍可能破坏 tool-call/tool-result 结构，因此每批 patches 提交前必须验证最终 projection。

## 14. 非连续 replace 的具体语义

非连续 replace 是本 ADR 的核心能力之一。

假设当前 projection：

```text
m1, m2, m3, m4, m5, m6
```

Patch：

```python
ReplacePatch(
    target_ids=["m1", "m3", "m6"],
    replacement_items=[summary],
)
```

结果：

```text
summary, m2, m4, m5
```

其中：

```text
m1/m3/m6 原消息仍在 ledger
projection 中被 TapeAnchor.suppresses 隐藏
summary 是 middleware synthetic message
summary 逻辑位置是 m1 的位置
```

如果之后另一个 middleware 继续压缩：

```text
current projection:
summary, m2, m4, m5
```

它可以合法 target：

```text
summary, m2
```

因为 `summary` 已经是当前 model-visible message。

但它不能再 target `m1`，因为 `m1` 已经不在当前 model-visible projection 中。

## 15. Migration plan

### Step 1：修正 ADR 状态

本 ADR 当前保持：

```text
Status: Proposed
```

等实现和测试收敛后再改为 Accepted。

### Step 2：调整 MessageMiddleware 接口

从：

```python
async def process(
    self,
    ctx: MessageMiddlewareContext,
    tape: MessageTape,
) -> list[MessageTapePatch]:
    ...
```

改为：

```python
async def process(
    self,
    ctx: MessageMiddlewareContext,
    messages: tuple[TapeMessage, ...],
) -> list[MessageTapePatch]:
    ...
```

Runtime 在调用 middleware 前负责：

```python
messages = tuple(tape.model_visible())
```

### Step 3：扩展 MessageMiddlewareContext

增加：

```python
turn_start_id: str | None = None
```

由 runtime 在 turn lifecycle 中填充。

同时删除当前未被使用的 `ContextBudget` / `MessageMiddlewareContext.budget`。如果后续 compaction 或 condensation 真需要预算输入，应为具体 middleware 设计明确参数，而不是保留通用但没有读者的 context 字段。

### Step 4：保留 InsertPatch

不要删除：

```text
InsertPatch
InsertPosition
```

但调整文档语义：

```text
InsertPatch 是 projection insert，不是 ledger mutation。
InsertPatch.position 允许为 None，表示 append 到当前 projection 尾部。
删除 TapePosition，改用 InsertPosition(kind=before/after, target_id=...)。
InsertPatch / ReplacePatch 不携带 operation 字段；operation 只存在于编译后的 durable MessageRewriteAnchor。
```

### Step 5：ReplacePatch 支持非连续 target

删除 runner 和 ledger 中的 contiguous target 校验。

保留并加强：

```text
target 非空
target 无重复
target 当前可见
同批 patch target 不重叠
replacement 非空
```

### Step 6：RunLedger.apply_many 支持新事务不变量

Ledger 必须按事务开始时的 current projection 校验一批 patches，而不是假设 replace target 构成连续 span。

Insert target 也必须按事务开始时的 current projection 校验，而不是只查 historical `message_ids`。已经被 suppress 的 message id，即使仍存在于 ledger，也不能作为 `InsertPosition.target_id`。

### Step 7：Metadata 随 synthetic messages 投影

修改 patch 编译逻辑，使 `patch.metadata` 的 semantic 部分写入 `MessageRewriteMessageDraft.metadata`。

InsertPatch 和 ReplacePatch 都应如此。

不引入 per-message metadata。Patch-level metadata 会复制到该 patch 生成的每条 synthetic message；需要不同 metadata 的 synthetic messages 必须拆成多个 patches。

保留当前 generated-field 边界：`MessageRewriteMessageDraft` 不包含 `message_id`，ledger 生成 stored `MessageRewriteMessage.message_id`。

删除 `context.py` fold 后 `MessageRewriteRecord.rewrite_id` / `MessageRewriteRecord.middleware` 字段。`MessageRewriteRecord` 只保留 projection 需要的 `operation`、`position`、`suppresses`、`messages`。

这不删除 durable event / ledger validation 中的 `rewrite_id`：ledger 仍需要它把 begin/message/end 组成同一个 rewrite block、生成 replacement message id、校验 begin/end 匹配，并支持 `rewrite_audit()`。durable `MessageRewriteAnchor.middleware` 也仍保留，用于 ledger begin/end 校验和 rewrite audit。

保留 durable `MessageRewriteAnchor.kind = begin/end`。它是 rewrite block framing，不是 patch API 上的可选装饰。

把 `MessageRewriteAnchorDraft` / `MessageRewriteMessageDraft` 视为 ledger/internal API：middleware 和业务层通过 patches 表达意图，由 runner 负责编译 rewrite draft block。不做强封装，但文档 contract 不鼓励上层手写 rewrite blocks。

同步更新 `rewrite_audit()` 输出 contract：删除 anchor-level `metadata`，只在 replacement messages 上暴露 message metadata。

### Step 8：改造 SkillReminderMiddleware

旧语义：

```text
BEFORE_MODEL_REQUEST
durable=False
conversation_start
每次请求提醒
```

新语义：

```text
AFTER_USER_MESSAGE_COMMITTED
before ctx.turn_start_id
middleware policy decides whether to skip
metadata carries category/catalog_digest/version
```

可以保留类名，但实现语义必须改变。middleware 只返回普通 `InsertPatch`；runner 负责把 patch 持久化为 projection events，patch 自身不再携带 `durable` 字段。

### Step 9：删除 SkillChangeNoticeMiddleware

删除 `SkillChangeNoticeMiddleware` class、runtime exports、builder wiring 和相关测试引用，不保留 deprecated alias。

不要再使用 `AFTER_TURN_CLOSED` + turn-end append（旧 `conversation_end` notice）来表达 skill catalog change。

### Step 10：处理 AGENTS.md

删除 runtime `AgentsMDMiddleware`。`AGENTS.md` 读取、缓存、热更新和文件路径策略属于 `knuth-cli` / host；runtime 只接收 host 注入的 `SystemSectionProvider`。

直接删除 `AgentsMDMiddleware` class、runtime exports、builder wiring 和相关测试引用，不保留 deprecated alias。保留 alias 会让 runtime 继续显得拥有 `AGENTS.md`，与本 ADR 的 ownership 边界冲突。

本 ADR 不设计 `AGENTS.md` hot reload。如果未来需要，应作为 host project instruction feature 单独决策，而不是重新放回 runtime `MessageMiddleware`。

### Step 11：移除 patch durability 分支

删除 `InsertPatch.durable` / `ReplacePatch.durable` 字段。目标态中，middleware patch 一律表示需要进入 ledger 的 projection event plan。

`MessageMiddlewareRunner.__init__` 校验 middleware names 在 runner 内唯一；重复 name 直接 `ValueError`。

最终接受态应删除 `ephemeral_records` 这条模型上下文路径，并删除 runner 中 durable / ephemeral 的分支。迁移过程中可以短暂保留旧 API 以便分步改造，但它不是目标设计的一等能力，也不应承载任何会影响模型行为的 synthetic conversation message。

`MessageMiddlewareRunner.run_checkpoint(...)` 目标态不返回 `MessageMiddlewareRunResult`，也不返回任何会被 `ContextBuilder.build(...)` 消费的 rewrite records。它可以返回 `None`；如果需要 debug / metrics，另行返回不参与 context build 的 stats。

删除 `MessageMiddlewareRunner.assert_checkpoint_complete(...)` public API。模型请求前置条件检查如果需要，应作为 `run_checkpoint(BEFORE_MODEL_REQUEST)` 的内部流程或 runner 内部 helper，而不是暴露第二条 checkpoint API。`ObservationCondensationMiddleware` 的“超长 tool result 不得残留”检查属于这个内部流程。

删除 `ContextBuilder.build(ephemeral_rewrite_records=...)` 参数。`ContextBuilder` 目标态只从 ledger durable projection 和 stable `SystemSectionProvider` preamble 构造模型输入，不接受调用方提供的临时 rewrite records。

最终目标是：

```text
model-seen synthetic messages are durable projection events
```

## 16. 测试计划

### 16.1 非连续 replace

输入：

```text
m1, m2, m3, m4, m5
```

Patch：

```text
[m1, m3, m5] -> [s1]
```

期望：

```text
s1, m2, m4
```

### 16.2 target_ids 顺序不影响结果

```text
[m1, m3] -> [s1]
[m3, m1] -> [s1]
```

结果应一致。

### 16.3 重复 target 被拒绝

```text
target_ids = [m1, m1]
```

应报错。

### 16.4 同批 patches overlap 被拒绝

```text
P1 target_ids = [m1, m3]
P2 target_ids = [m3, m5]
```

应报错。

### 16.5 replacement_items 为空被拒绝

```text
target_ids = [m1]
replacement_items = []
```

应报错。

### 16.6 insert before current turn

当前 projection：

```text
m1, m2, m3
ctx.turn_start_id = m3
```

Patch：

```text
InsertPatch(before m3, [skill_reminder])
```

期望：

```text
m1, m2, skill_reminder, m3
```

### 16.7 skill reminder 不插 conversation_start

确认 skill reminder 不再出现在整个 conversation 开头，而是出现在当前 turn start 前。

### 16.8 skill reminder cache behavior

已有：

```text
system, turn1, turn2, turn3
```

turn4 前 skill catalog 变化。

期望 projection：

```text
system, turn1, turn2, turn3, skill_reminder, turn4
```

turn1-turn3 的 prefix 不变。

### 16.9 skill reminder skip policy

如果 catalog digest 未变化，middleware 返回空 patches。

如果 catalog 变化但策略判断与当前 turn 无关，可以返回空 patches。

### 16.10 metadata 可识别

注入 skill reminder 后，projection 中对应 `TapeMessage.metadata.semantic` 应包含：

```text
category = skill_reminder
catalog_digest
snapshot_version
```

Middleware 不需要正则解析 content。

### 16.11 durable replay

skill reminder 一旦进入模型输入，ledger replay 后必须能重建同样的 context。

### 16.12 middleware 接收 model-visible messages

构造已有 replace summary 后，再运行第二个 middleware。第二个 middleware 收到的 messages 应包含 summary，不包含被 suppress 的原 messages。

### 16.13 insert target validation

`InsertPatch(before target_id)` 中，如果 target 不在当前 projection，应被 ledger 拒绝。

### 16.14 provider message validation

插入或替换后，最终 message sequence 仍必须满足 provider 结构约束。

## 17. 非目标

本 ADR 不处理：

```text
删除 TapeAnchor
引入 messages_by_id / visible_order / ledger_order
支持纯删除 rewrite
支持 replacement 自定义 anchor_id
设计完整 patch algebra
让 middleware target system preamble
每次请求动态重写 system prompt
```

这些问题如果未来需要，单独决策。

## 18. 最终结论

本 ADR 将 `MessageMiddleware` 重新定义为：

```text
append-only ledger 上的 projection event producer
```

而不是狭义的 rewrite hook。

最终模型是：

```text
Ledger
    append-only durable events

Middleware
    基于当前 model-visible messages 生成 projection patches

Runner
    校验 patches
    编译成 append-only anchor/message events
    写入 ledger
    重建 projection

MessageTape
    使用 TapeMessage + TapeAnchor 构造 raw/model 两种视图

ContextBuilder
    拼接 stable system preamble 和 model-visible conversation
```

核心决策：

```text
InsertPatch 保留
ReplacePatch 支持非连续 target
TapeAnchor 保留
replacement 放在最早 target 位置
middleware 不接收 MessageTape
middleware 接收当前 model-visible messages
skill reminder 由 middleware 决策
skill reminder 锚定在当前 turn start 前
skill reminder 一旦注入就由 runner 持久化
conversation.notice 不承担需要定位的 reminder 注入
动态状态不放 conversation_start，不反复改写 system preamble
cache-first 是 harness 的一等设计原则
```

这套设计既保留了 append-only ledger 的审计和 replay 能力，也让 middleware 能够表达 insert、replace、append 等 projection 行为，同时避免动态状态破坏 prompt prefix cache。
