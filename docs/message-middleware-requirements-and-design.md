# MessageMiddleware 需求与设计

状态：Proposed
日期：2026-06-16
依据：[CONTEXT.md](../CONTEXT.md)、[ADR-003](decisions/ADR-003-system-preamble.md)、[ADR-004](decisions/ADR-004-runtime-control-and-agent-loop-boundaries.md)

本文重新定义 Knuth 中 `MessageMiddleware` 的含义和能力。旧定义把它描述为 `ContextView` 的 full-power rewriter，但这个说法过粗：它既没有表达 AgentsMD、ContextCompaction、ToolResultRedaction 这些真实诉求，也容易让实现滑向“在内存里临时删改 messages”。

新的定义是：

> `MessageMiddleware` 是绑定到 run 生命周期 checkpoint 的 `MessageTape` rewrite 组件。它在安全时机产生结构化 tape patches，通过 internal anchors 和 model-visible replacement messages 表达注入、遮蔽、替换、压缩和 redaction。`ContextBuilder` 负责把 base conversation、durable rewrites 和 ephemeral injections 投影成 provider-valid 的 `InferenceMessage` 列表。

## 目标

- 支持 `AgentsMD`：把适用的 `AGENTS.md` 注入模型上下文。
- 支持 `ContextCompaction`：在 turn 结束等安全时机判断是否压缩历史，并记录可恢复的压缩结果。
- 支持 `ToolResultRedaction`：在工具结果提交后为 context headroom 做工具结果瘦身，同时保持 tool-call / tool-result 序列合法。
- 用结构化 anchor 表达“改写”，而不是在内存里偷偷删除或覆盖消息。
- 让 run 暂停、恢复、跨进程继续时能稳定解释已经发生过的压缩或 redaction。
- 为未来第三方插件自定义 message middleware 留出边界，但第一版不设计完整插件 ABI。

## 非目标

- 不把 middleware 变成 run state、approval、tool invocation 或 policy 的控制面。
- 不让 middleware 直接写 ledger；middleware 返回 patch，由 runner 校验和持久化。
- 不设计通用 AST patch language；第一版只支持 insert / replace 这类消息级操作。
- 不要求所有 middleware 都产生 durable rewrite。`AgentsMD` 这类 build-time injection 可以是 ephemeral。
- 不重新设计 provider message schema。最终仍输出现有 `InferenceMessage`，并在 projection 阶段保证 provider-valid。

## 核心概念

### MessageTape

`MessageTape` 是构造模型输入时使用的中间表示。它不是 provider API，也不是 durable ledger 本身，而是从 durable events 和 rewrite records fold 出来的、带稳定 id 的消息序列。

每个 tape item 至少包含：

```python
class TapeMessage(KnuthModel):
    id: str
    role: Literal[
        "system",
        "user",
        "assistant",
        "tool_result",
        "internal_anchor",
    ]
    content: str | None = None
    tool_calls: list[ToolCall] = []
    tool_call_id: str | None = None
    tool_name: str | None = None
    origin: Literal["ledger", "middleware"]
    source_event_seq: int | None = None
    middleware_name: str | None = None
    visibility: Literal["model", "internal"] = "model"
    metadata: dict[str, Any] = {}
```

`id` 必须稳定。ledger 事件投影出的消息可以使用事件 seq / event id 派生；middleware 生成的 replacement message 可以使用 `rewrite_id + position` 派生。稳定 id 是 `suppresses` 能跨 resume 工作的前提。

### Internal Anchor

anchor 是 `visibility=internal` 的 tape item。它不会进入模型输入，但会告诉 projection 层如何解释某段 rewrite。

典型 compaction anchor：

```text
a:100 harness.middleware.compaction.begin
      rewrite_id = "compact:..."
      middleware = "context_compaction"
      operation = "replace"
      suppresses = ["m:001", "m:002", "m:003"]
      algorithm = "rolling_summary_v1"
      original_hash = "..."
      original_chars = 18000
      replacement_chars = 900
mw:101 user "Earlier context summary: ..."
a:102 harness.middleware.compaction.end
      rewrite_id = "compact:..."
```

最终 projection 会：

- 跳过 internal anchors。
- 跳过被 `suppresses` 指向的原始消息。
- 保留 replacement messages。
- 校验最终序列是否 provider-valid。

这里使用 `suppress` / `suppressed_by` 语义，不使用 `cancel`，避免和 run cancellation 混淆。原始消息仍是历史事实，只是不进入本次模型输入。

### MessageTapePatch

第一版只需要两个操作：

```python
class TapePosition(KnuthModel):
    kind: Literal["before", "after", "boundary"]
    target_id: str | None = None
    boundary: Literal["conversation_start", "conversation_end", "before_model_request"] | None = None


class InsertPatch(KnuthModel):
    operation: Literal["insert"]
    position: TapePosition
    items: list[TapeMessage]
    durable: bool = False
    metadata: dict[str, Any] = {}


class ReplacePatch(KnuthModel):
    operation: Literal["replace"]
    target_ids: list[str]
    replacement_items: list[TapeMessage]
    durable: bool = True
    metadata: dict[str, Any] = {}
```

`TapePosition` / target span 必须是 durable 语义的一部分。ledger append 顺序只表示 rewrite 什么时候发生，不总是表示 replacement 在逻辑消息序列里应该出现在哪里。`ToolResultRedaction` 必须在 tool result 后、下一次模型请求前同步写入，所以它的 rewrite events 会自然落在对应 tool result 附近；但仍要记录 `target_ids` 来 suppress 原始 tool result 并保留 tool-call / tool-result 配对。`ContextCompaction` 这类 turn 后或恢复兜底 rewrite 可能在很晚之后写入，必须靠 durable target span 把 summary 放回被替换历史 span 的起点，而不是按 append seq 追加到对话尾部。

`insert` 必须显式声明插入位置。`replace` 的插入位置由 `target_ids` 决定：replacement messages 插入到 target span 的起点。第一版要求 `target_ids` 是当前 projected tape 中的连续 span；单条 tool result replacement 是这个规则的特例。

`replace` 由 middleware runner 编译成一组 durable rewrite events，并在 refold 时投影成 tape items：

```text
message.rewrite_anchor(kind="begin")
message.rewrite_message(...)
...
message.rewrite_anchor(kind="end")
```

`metadata` 至少应能携带：

- `rewrite_id`
- `middleware`
- `algorithm` / `version`
- `reason`
- `original_hash`
- `original_chars`
- `replacement_chars`

后续如果需要纯 metadata 标注，可再加 `annotate`，第一版不需要。

## 运行时机

`MessageMiddleware` 不应该只在 `ContextBuilder.build()` 里临时运行一次。不同能力有不同的安全时机：

- `ToolResultRedaction` 应在工具结果正常提交后处理。
- `ContextCompaction` 应在每个 turn 结束时判断是否压缩。
- `AgentsMD` 更适合在构造模型输入前做 ephemeral injection。

因此 middleware 需要绑定生命周期 checkpoint。

第一版 checkpoint 只保留三个：

```python
class MessageMiddlewareCheckpoint(StrEnum):
    AFTER_TOOL_RESULT_COMMITTED = "after_tool_result_committed"
    AFTER_TURN_CLOSED = "after_turn_closed"
    BEFORE_MODEL_REQUEST = "before_model_request"
```

### AFTER_TOOL_RESULT_COMMITTED

工具结果已经作为 durable fact 写入 ledger 后、agent loop 发起下一次模型请求前触发。这个 checkpoint 是阻塞式 critical-path gate，用于处理单个或最近一批 tool result 的 payload 压力。agent loop 不允许在这个 checkpoint 完成前继续 `ContextBuilder.build()` / model request；否则模型已经基于原始 tool result 推理，事后 redaction 只能影响未来请求，不能改变已经生成的答案。

这个 checkpoint 必须绑定到**统一的 tool completion commit path**，而不是某一个 loop 分支。任何路径只要追加 `ToolInvocationCompletedDraft`，都必须经过同一个 post-commit redaction checkpoint，例如本地工具执行完成、client/external tool result submit、unknown outcome 人工 resolve、denied backfill 或 crash recovery 产生的 completion。`BEFORE_MODEL_REQUEST` 还必须有兜底断言：当前 open/closed batch 中若存在需要 redaction 但尚未处理的 tool result，不能进入 `step.started`。

`ToolResultRedaction` 在这里运行：

```text
tool.invocation_completed 写入 ledger
↓
AFTER_TOOL_RESULT_COMMITTED
↓
middleware 读取当前 tape 和 artifact 内容
↓
若 observation / artifact 过大，返回 ReplacePatch
↓
runner 校验 replacement tool_result 保持相同 tool_call_id / tool_name
↓
写入 durable rewrite
↓
重新构造下一次模型请求上下文
```

这个时机不改变工具状态。工具已经 completed；redaction 只改变紧接着的下一次模型请求以及未来请求看到的 tool result 内容。如果 redaction 因预算压力是必需的但失败，agent loop 不能静默继续用超大原始结果发模型；应失败、暂停或走明确 fallback。

多工具 batch 需要区分两层校验：某个 tool result 完成后，batch 可能还没有收齐所有 tool observations，此时完整 provider-visible message 序列天然还不合法。`ToolResultRedaction` 在这个阶段只做 partial-batch structural validation：replacement 必须仍是同一个 `tool_call_id` / `tool_name` 的 `tool_result`，且不破坏已完成部分的顺序。真正发送模型前，在 batch closed 且即将 `step.started` 时再对完整 projection 做 provider-valid 校验。

### AFTER_TURN_CLOSED

一个 agent turn 已经结束后触发。这里的 turn closed 指：

- 当前用户输入触发的一轮 agent work 已完成。
- 没有 open tool batch。
- 没有 `WAITING_APPROVAL` 或 `WAITING_TOOL_RESULT`。
- run 已进入等待下一次用户输入的状态，例如 `SUCCEEDED`。

`ContextCompaction` 应主要在这里运行。理由：

- 当前回答已经生成，压缩不会影响本轮推理。
- tool-call / tool-result 配对已经闭合。
- 压缩结果能在下次 continue / resume 前稳定存在。

这个 checkpoint 的 crash 语义必须明确：`AFTER_TURN_CLOSED` 可以在 `RunSession` 成功收尾路径中 shield/await，尽量在本次进程结束前完成；但它不能成为唯一保证。若进程在 `run.succeeded` 之后、compaction 写入之前退出，下一次 `continue_run` / `BEFORE_MODEL_REQUEST` 前必须幂等补跑 compaction 检查。换句话说，turn-closed compaction 是首选时机，before-model-request 是恢复兜底。

流程：

```text
model.completed final answer
run.succeeded
↓
AFTER_TURN_CLOSED
↓
ContextCompaction 检查 token pressure / history age / message count
↓
选择 provider-valid 的旧消息 span
↓
通过 deterministic strategy 或窄 `Summarizer` port 生成 summary / replacement messages
↓
返回 durable ReplacePatch
↓
runner 写入 compaction anchors
```

### BEFORE_MODEL_REQUEST

每次模型请求前触发。这个 checkpoint 不应成为常规 durable rewrite 的主要时机，但有两个必要用途：

- `AgentsMD` 这类 ephemeral context injection。
- 兜底：如果马上要发给模型时发现仍超预算，可以触发 emergency compaction。

兜底 compaction 必须由 agent loop / run invocation 层先写 durable rewrite，然后再调用纯 projection 的 `ContextBuilder.build()`。不能在同一次 build 里生成一个不落盘的 summary 直接发给模型，否则 pause / resume 后会看到不同上下文。

## MessageMiddleware 接口

第一版接口应保持窄，不给 middleware 暴露 runtime 全能上下文。

```python
class MessageMiddleware(ABC):
    name: str
    priority: int = 100
    checkpoints: set[MessageMiddlewareCheckpoint]

    @abstractmethod
    async def process(
        self,
        ctx: MessageMiddlewareContext,
        tape: MessageTape,
    ) -> list[MessageTapePatch]:
        ...
```

`MessageMiddlewareContext` 只包含 middleware 做消息改写判断所需的只读事实和能力：

```python
class MessageMiddlewareContext(KnuthModel):
    run_id: str
    checkpoint: MessageMiddlewareCheckpoint
    budget: ContextBudget | None = None
```

```python
class ContextBudget(KnuthModel):
    max_input_tokens: int
    reserved_output_tokens: int
    target_headroom_tokens: int
```

v0 的 `budget` 可以为空。内置 redaction / compaction 先使用字符长度阈值作为保守启发式，并在 metadata 中记录 `*_chars`。当 runtime 拥有稳定 tokenizer 与预算来源后，再把 token 计数能力加回 `MessageMiddlewareContext`；不要先暴露一个恒为 None 或无法兑现的 token API。工具 artifact 的读取由 runner 在重建 `MessageTape` 时通过 ledger 完成，middleware 看到的是已经解析好的 tape，不直接持有 artifact reader。

刻意不放入这些字段：

- `run_status`：runtime 负责只在安全 checkpoint 调用 middleware，middleware 不自己判断 run lifecycle。
- `open_batch` / `pending_approvals` / `waiting_tool_result`：这些是 runtime orchestration 状态，不应成为 middleware API。
- `rewrite_writer`：middleware 不写库，只返回 patch。
- `model_config_fingerprint`：snapshot/freeze 层关心；middleware 需要的是预算而不是完整模型配置。
- `inference_client`：middleware 不应拿完整模型调用面。`ContextCompaction` 若需要 LLM summary，应在构造时注入一个窄的 `Summarizer` port；第一版也可以先只支持 deterministic / stub summarizer。
- `phase`：checkpoint 已经表达运行时机，先不引入第二套调度概念。

边界原则：

```text
状态判断留在 runtime
消息改写留给 middleware
持久化留给 middleware runner / ledger
```

## Durable 与 Ephemeral

middleware 可以有对象状态，但要区分缓存状态和语义状态。

可以留在 middleware 实例内的缓存：

- AGENTS.md 文件内容缓存。
- token estimate cache。
- artifact 文本读取缓存。

必须 durable 的语义状态：

- 哪些消息已经被 compact。
- 某个 tool result 是否已经 redacted。
- summary / replacement 的具体内容。
- rewrite 使用的 algorithm / version / hash。

原因很简单：进程重启、pause / resume、跨进程 continue 都不能依赖 Python 对象内存。middleware 重启后应通过 tape 上已有 anchors 判断“我是否已经处理过这段历史”，而不是靠实例字段。

## ContextBuilder 新流程

`ContextBuilder.build()` 不再是“运行所有 middleware 并直接返回 view”。它的职责应收缩为纯 tape projection，不能写 ledger，不能触发 durable compaction，也不能产生 durable rewrite。会写 durable events 的 checkpoint runner 属于 `AgentLoop` / `RunInvocation` orchestration 层。

1. 从 ledger events fold 出 base conversation tape。
2. 读取 durable rewrite anchors 和 replacement messages，合并进 tape。
3. 接收调用方已经准备好的 ephemeral patches，例如本次 `AgentsMD` insertion。
4. projection：
   - 收集所有 anchors 中的 `suppresses`。
   - 跳过 internal anchors。
   - 跳过被 suppress 的原始消息。
   - 保留 replacement 和 ephemeral model-visible messages。
5. 做 provider-valid 校验。
6. 合并或排列 system/context 注入，满足 provider 要求。
7. 生成 `ContextSnapshot`。

`ContextBuilder` 不调用会产生 durable patch 的 middleware runner。`BEFORE_MODEL_REQUEST` 若需要 emergency compaction，调用方必须先运行 durable checkpoint runner、通过 ledger 提交 rewrite events，然后重新调用 `ContextBuilder.build()`。`ContextView` 是 projection 结果，不是 rewrite 工作区。

## 内置 Middleware

### AgentsMD

职责：把当前适用的 `AGENTS.md` 注入模型上下文。

运行时机：`BEFORE_MODEL_REQUEST`。

durability：默认 ephemeral。后续如果需要审计“某次模型请求使用了哪个 AGENTS.md”，可以在 `ContextSnapshot` 或 debug artifact 中记录 hash，不必把全文写入 conversation ledger。

patch 形态：

```text
a:agents_md.begin
      operation = "insert"
      source_path = "/.../AGENTS.md"
      content_hash = "..."
mw:agents_md system/context "...AGENTS.md content..."
a:agents_md.end
```

如果 provider 只接受 leading system message，projection 层负责把这类 system/context injection 与 base preamble 合并成一条 leading system message。不要把多个 system message 随意插在 conversation 中间。

### ToolResultRedaction

职责：为 context headroom 缩小过大的 tool result，同时保持工具对话结构合法。

这里的 redaction 是 **context-size redaction / projection redaction**，不是 security redaction。原始 observation / artifact 已经作为 durable fact 保存，不能靠后续 rewrite 清除秘密。secret、token、credential 等安全脱敏必须发生在 append 之前，走 `EventRedactor`、工具输出预处理或 artifact pre-write redaction；`ToolResultRedaction` 只决定模型输入是否使用较短的 replacement。

运行时机：`AFTER_TOOL_RESULT_COMMITTED`。

durability：durable。否则同一个巨大 tool result 在每次 build 时都可能被不同策略重写，resume 行为不稳定。

处理流程：

```text
m:030 assistant tool_calls=[call_search]
m:031 tool_result call_search huge output
a:040 harness.middleware.tool_result_redaction.begin
      operation = "replace"
      suppresses = ["m:031"]
      original_sha256 = "..."
      original_chars = 48000
      replacement_chars = 1200
      reason = "context_headroom"
mw:041 tool_result call_search "Result redacted for context headroom. Relevant excerpt: ..."
a:042 harness.middleware.tool_result_redaction.end
m:043 step.started   # 下一次模型请求看到 redacted tool_result，而不是原始 huge output
m:044 assistant "..."
```

projection 后：

```text
m:030 assistant tool_calls=[call_search]
mw:041 tool_result call_search "Result redacted..."
m:044 assistant "..."
```

约束：

- replacement 必须仍是 `tool_result`。
- replacement 必须保留同一个 `tool_call_id`。
- replacement 应保留 `tool_name`。
- 原始 observation / artifact 不删除；只是被模型输入 suppress。
- 如果 redaction 需要读取 artifact，由 runner 通过 ledger 重建 `MessageTape` 时解析；middleware 不绕过 ledger 直接读外部存储。
- 不得把它用于 secret cleanup；如果原始 tool result 含 secret，说明 append 前 redaction 边界已经失败。
- redaction rewrite 必须发生在下一次 `step.started` / model request 之前；如果已经有后续 assistant answer，再追加 redaction 只能影响未来 turn，不能声称影响那条 answer 的推理输入。

### ContextCompaction

职责：把旧消息 span 替换成 summary / condensed messages。

主要运行时机：`AFTER_TURN_CLOSED`。

兜底运行时机：`BEFORE_MODEL_REQUEST`，仅在马上超预算时使用，并且必须 durable 写入后重新 build。

durability：durable。尤其是 LLM 生成 summary 时，summary 文本必须记录下来；不能每次 build 重新生成。

处理流程：

```text
AFTER_TURN_CLOSED
↓
读取当前 tape
↓
排除已经被 suppress 的消息
↓
选择一个 provider-valid 的旧 span
↓
生成 replacement summary
↓
返回 durable ReplacePatch
↓
runner 写入 anchors
```

provider-valid span 约束：

- 不能只 compact 掉 assistant tool call 而留下对应 tool result。
- 不能留下 dangling tool result。
- 如果 span 涉及一个 tool batch，要么整个 batch 被替换，要么完全不碰。
- 不 compact 当前刚结束 turn 的 final answer，除非策略明确允许并且有测试覆盖。

anchor metadata 至少包含：

```text
rewrite_id
middleware = "context_compaction"
algorithm / version
suppress target ids
original_hash
original_chars
replacement_chars
summary_hash 或 artifact_ref
```

## Middleware Runner

需要一个 runtime 内部 runner 负责调用 middleware、校验 patch、写入 durable rewrite。

职责：

- 按 checkpoint 选择 middleware。
- 按 `priority` 排序。
- 给每个 middleware 提供只读 `MessageMiddlewareContext` 和当前 `MessageTape`。
- 校验 patch：
  - target ids 存在。
  - target ids 尚未被其它 rewrite suppress，或冲突可被明确拒绝。
  - `replace.target_ids` 是连续 span，且 replacement 插入到 span 起点后不会破坏局部结构。
  - replacement messages 自身形状合法。
  - durable patch 具备 rewrite id 和必要 metadata。
- 将 durable patch 编译为 `message.rewrite_anchor` / `message.rewrite_message` 事件序列，并写入 ledger。
- 将 ephemeral patch 返回给 `ContextBuilder` 做本次 projection。
- 对 patch 应用后的 tape 做 dry-run projection。若 checkpoint 处于完整 conversation boundary，例如 `BEFORE_MODEL_REQUEST` 或 `AFTER_TURN_CLOSED`，必须对最终 `InferenceMessage` 列表跑 provider-valid validator。若 checkpoint 位于 open tool batch 中，例如多工具 batch 的某个 `AFTER_TOOL_RESULT_COMMITTED`，只做 partial-batch structural validation；完整 provider-valid 校验推迟到下一次 `step.started` 前。

冲突处理第一版从严：

- 两个 middleware 不能 durable replace 同一条 target message。
- 如果检测到 overlap，后运行的 middleware 必须跳过或报错；不要隐式叠加。
- middleware 应通过 tape 上已有 anchors 判断自己已经处理过的 span，避免重复压缩。

runner 的校验只是第一层。真正的 durable 不变量必须在 `RunLedger.apply_many(...)` 的同一事务内重新验证：`rewrite_id` 唯一、begin/end 配对、replace target 尚未被 active rewrite suppress、message ids 不重复。否则两个进程可能基于同一份旧 tape 同时生成 patch，分别通过 runner 预校验后写入冲突 rewrite。

## Durable 表达

第一版选择新增 durable message rewrite events。一个 durable patch 不落成单个大 blob，而是落成一组有顺序的事件：

```text
message.rewrite_anchor(kind="begin")
message.rewrite_message(...)
message.rewrite_message(...)
...
message.rewrite_anchor(kind="end")
```

这组事件是 rewrite 的 durable 事实。refold 时，begin / end anchor 事件投影成 `visibility=internal` 的 `TapeMessage`；中间的 `message.rewrite_message` 事件投影成 middleware 生成的 model-visible replacement / inserted messages。

### message.rewrite_anchor

`message.rewrite_anchor` 表达 rewrite span 的边界和规则。第一版字段建议：

```python
class MessageRewriteAnchorDraft(RuntimeEventDraftBase):
    type: Literal["message.rewrite_anchor"] = "message.rewrite_anchor"
    rewrite_id: str
    kind: Literal["begin", "end"]
    middleware: str
    operation: Literal["insert", "replace"]
    position: TapePosition | None = None
    suppresses: list[str] = []
    metadata: dict[str, Any] = {}
```

`kind="begin"` 的 anchor 携带主要规则，尤其是 `operation`、`position`、`suppresses`、algorithm / version / token counts / hashes。`insert` 必须带 `position`；`replace` 的 `position` 可以显式记录 target span 起点，或由 `suppresses` 的第一个 target 推导，但 refold 规则必须确定且唯一。`kind="end"` 用同一个 `rewrite_id` 关闭这段 rewrite，可携带 output count、replacement hash 或校验信息。

这些事件必须正式加入 `knuth-core` 的 draft / stored event union、`knuth.core.events` facade、serialization registry，以及 `RunLedger` reducer。reducer 对 run 状态可以是 no-op，但不能是无校验 no-op：它必须维护或重建 message rewrite projection，用于事务内验证 rewrite 不变量。

### 允许写入状态

`message.rewrite_*` 是上下文投影事实，不是 run lifecycle 事件。它只推进 run 的事件 seq / updated-at，不改变 `run.status`。第一版按 checkpoint 限定允许状态：

| checkpoint | 允许状态 / batch 条件 | 说明 |
|---|---|---|
| `AFTER_TOOL_RESULT_COMMITTED` | 刚追加 `tool.invocation_completed`；run 不能是 `FAILED` / `CANCELLED`；batch 可以仍 open，也可以即将 closed | 用于 projection redaction。可发生在 active local tool、external/client tool result submit、unknown resolve 或 denied backfill 之后。 |
| `AFTER_TURN_CLOSED` | `SUCCEEDED` 且无 open batch | 用于 turn 后 compaction。`SUCCEEDED` 在 Knuth 中仍可被 `continue_run` 追加新用户消息，因此允许写入上下文整理事实。 |
| `BEFORE_MODEL_REQUEST` durable fallback | 即将写 `step.started`，无 open batch，run 正由 live invocation 推进；不能是 `FAILED` / `CANCELLED` | 用于 emergency compaction 或补跑未完成的 redaction / compaction。写入后重新 build，再进入 `step.started`。 |

任何 `FAILED` / `CANCELLED` 之后的 `message.rewrite_*` 都应拒绝。`PAUSED` / `WAITING_APPROVAL` / `WAITING_TOOL_RESULT` 下不应由后台维护任务任意写 rewrite；只有在对应 runtime control 正在提交 tool completion 或恢复到下一次模型请求的同步路径上，才通过上表 checkpoint 写入。

### message.rewrite_message

`message.rewrite_message` 表达 middleware 生成的一条 model-visible 消息。第一版字段建议：

```python
class MessageRewriteMessageDraft(RuntimeEventDraftBase):
    type: Literal["message.rewrite_message"] = "message.rewrite_message"
    rewrite_id: str
    index: int
    message: InferenceMessage
    message_id: str
    metadata: dict[str, Any] = {}
```

如果 replacement 内容很大，`message` 可以改为 artifact 引用或让 `content` 外置；但这只是 payload 存储优化，不改变 durable 表达：ledger 中仍必须有 `message.rewrite_message` 事件说明这条 replacement message 存在、属于哪个 `rewrite_id`、在 rewrite 内的顺序是什么。

### 原子性

一个 durable patch 对应的 rewrite event sequence 必须通过 `RunLedger.apply_many(...)` 或等价单事务写入。不能先写 begin anchor、稍后再写 replacement/end；否则 crash 可能留下半段 rewrite，projection 无法确定应该 suppress 哪些消息。

对 `ToolResultRedaction` 来说，`tool.invocation_completed` 可以先作为工具事实提交；随后 checkpoint 写入 redaction rewrite。但 redaction rewrite 自身的 begin / message / end 三段必须原子提交。

对 `ContextCompaction` 来说，summary 一旦生成，就必须和 begin/end anchors 一起作为同一个 rewrite sequence 提交。下次 resume / continue 只能 replay 这组事件，不能重新生成另一份 summary。

## Projection 规则

projection 是确定性的：

1. base messages 按 ledger seq 排序。
2. durable rewrite records 按 ledger seq 排序合入 tape。
3. internal anchors 不进入模型输入。
4. 若 message id 出现在任何 active rewrite 的 `suppresses` 中，该原始 message 不进入模型输入。
5. replacement messages 进入模型输入，除非它们自己被后续 rewrite suppress。
6. ephemeral insertions 只影响本次 build，不写入 ledger。
7. 最终序列必须通过 provider-valid 校验。

`ContextSnapshot.messages_hash` 应基于最终 projection 后的 `InferenceMessage` 列表计算。这样 snapshot 证明的是模型实际看到的上下文，而不是 ledger 原始历史。

`ContextSnapshot.preamble_hash` 需要明确语义。第一版建议让它表示最终 leading system message 的 hash，因此会包含 `SystemSectionProvider` 装配结果以及 `AgentsMD` 等 projection 合并进去的 system/context injection。若未来需要单独追踪“基础 SystemPreamble 是否变化”，再新增 `system_preamble_hash` 或 rewrite id 列表；不要让一个字段同时被两种解释使用。

## Read API 视图

rewrite projection 只定义模型输入，不自动改变所有 read API 的语义。第一版应显式区分这些视图：

- raw ledger conversation：从 durable conversation events 重建，展示原始 user / assistant / tool result / notice 历史，不应用 compaction/redaction suppress。
- live model context projection：`ContextBuilder` 在模型请求 critical path 上输出的实际输入，应用 durable rewrite、ephemeral injection、tool result redaction 和 compaction。
- durable model projection：只从 ledger durable events 投影，应用 durable rewrite/redaction/compaction，但不临时运行 AgentsMD 这类 ephemeral middleware，也不重新触发会写 ledger 的 checkpoint。`AgentRuntime.model_context_messages()` 第一版属于这个视图，适合调试 durable rewrite 是否生效，不等价于某一次 live model request 的完整输入。
- audit / rewrite view：展示 raw messages、rewrite anchors、replacement messages、suppression 关系和 metadata，供 debug / UI 检查“为什么模型看到了这份上下文”。

现有 `AgentRuntime.messages()` 若继续存在，不应暗中切到 model context projection。IM / AG-UI 历史展示要明确选择 raw conversation、redacted projection 或 audit view，避免把用户可见历史、审计历史和模型输入混成一个概念。

## 与 SystemSectionProvider 的关系

旧设计中 `SystemSectionProvider` 是“只贡献 preamble section，不能改 messages/tools”的 additive seam。这个边界仍然有价值，但需要调整表述：

- 稳定、全局、无位置敏感的系统提示仍可走 `SystemSectionProvider`。
- 需要根据 tape 状态、budget、tool result、历史 span 做结构性插入或替换的能力走 `MessageMiddleware`。
- `AgentsMD` 如果只是静态 preamble，可以走 provider；但当前诉求把它归为 middleware，是因为它要进入统一的 MessageTape rewrite / snapshot / future plugin 体系。

关键区别不是“谁能提供 system 文本”，而是：

```text
SystemSectionProvider 贡献 preamble fragment
MessageMiddleware 改写 MessageTape
```

## 实现顺序建议

第一步：引入 `MessageTape` 和 projection。

- 给现有 `reconstruct_messages_from_events()` 的输出包装稳定 id。
- 暂时不改变外部行为。
- `ContextView.messages` 仍由 projection 生成。

第二步：引入 durable rewrite event / record。

- 在 `knuth-core` 新增 `MessageRewriteAnchorDraft` / `MessageRewriteMessageDraft` 及 stored event 类型。
- 加入 `DurableRuntimeEventDraft`、`StoredRuntimeEvent`、`knuth.core.events.__all__` 和 serialization parse/store 路径。
- 在 `RunLedger` reducer 中加入 no-op but validating reducer，事务内验证 rewrite id、位置、target suppress 冲突和 begin/end 配对。
- 支持 `replace` 和 `insert`。
- 支持 internal anchors。
- projection 能 suppress target messages。

第三步：实现 `ToolResultRedaction`。

- 这是最局部的 rewrite：只替换单条 tool result。
- 先收口统一 tool completion commit path，确保所有 `ToolInvocationCompletedDraft` 写入口都会触发 redaction checkpoint。
- 支持 open batch 下的 partial-batch structural validation，并在下一次 `step.started` 前做完整 provider-valid 校验。
- 能验证 durable replay。

第四步：实现 `ContextCompaction`。

- 先只在 `AFTER_TURN_CLOSED` 运行。
- 先使用简单 deterministic summary 或人工 stub，跑通 durable rewrite；再接入窄 `Summarizer` port。不要把完整 inference client 放进 `MessageMiddlewareContext`。

第五步：实现 `AgentsMD`。

- 先作为 `BEFORE_MODEL_REQUEST` ephemeral insert。
- projection 层合并 leading system/context。

第六步：补 `BEFORE_MODEL_REQUEST` emergency compaction。

- 只在超预算时触发。
- 写入 durable rewrite 后重新 build。

## 验收场景

- tool result 超大时，本轮 tool completion 仍完整落 ledger；redaction rewrite 在下一次 `step.started` 前提交，紧接着的模型请求只看到 redacted replacement tool result。
- local tool completion、external/client `submit_tool_result`、unknown resolve、denied backfill 等路径都触发同一 redaction checkpoint。
- 多工具 batch 中第一条 tool result 被 redacted 时不要求完整 provider-valid；batch closed 后、下一次 `step.started` 前完整 projection 必须 provider-valid。
- `ToolResultRedaction` 只做 context-size/projection redaction；含 secret 的 tool output 必须在 append 前被拒绝或脱敏。
- redacted replacement 保留同一 `tool_call_id`，provider 不报 tool message sequence 错。
- turn 结束后触发 compaction；重启进程后继续 run，仍看到同一 summary，不重新生成。
- compaction 不会产生 dangling tool result。
- 在完整 conversation boundary 上，replacement patch 应用后的完整 projection 仍 provider-valid；覆盖整批 tool call/result replace 和 system/context merge 用例。
- 已 compact 的消息不会在下一次 build 中重新出现。
- AgentsMD 注入不会写入 durable conversation history，但会进入本次 `ContextSnapshot.messages_hash`。
- 两个 middleware 试图 replace 同一 target 时，runner 拒绝或后者跳过，不产生隐式叠加。
- 两个进程基于同一旧 tape 试图写入冲突 rewrite 时，ledger 事务内校验拒绝后写入者。
- `AgentRuntime.messages()` 或替代 read API 明确区分 raw conversation、model context projection 和 audit/rewrite view。
