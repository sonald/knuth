# ADR-002: 强类型事件语言

## 状态
Proposed

## 日期
2026-06-08

## 背景

Knuth 现在有两套事件：

- `InferenceEvent`：LLM 和 runtime 之间传递的底层模型流信息。
- `RuntimeEvent`：runtime 对外、对内用于 run timeline、恢复、审计、hook、debug 的事件语言。

旧实现用 `InferenceEventType` enum 加 `payload: dict` 表达模型流事件，用 `RuntimeEvent.namespace/name/type` 加 `payload: dict` 表达 runtime 事件。这种结构让字段含义只能靠约定维持，runtime、CLI、store 和测试都需要手写字符串和 payload key，且 `RuntimeEvent.namespace/name/type` 重复表达同一个事件身份。

## 决策

彻底重构事件系统，不保留旧事件 API 兼容层。

`InferenceEvent` 和 `RuntimeEvent` 都改成强类型 discriminated union，并使用显式 dotted `type` 字符串作为 canonical discriminant。`InferenceEventType` enum 不再作为长期 API 存在。

两套事件语言都放在 `knuth-core`。`knuth-llmd` 继续负责 provider chunk normalization 和产生 `InferenceEvent`，但 runtime 不应该为了共享事件类型 import `knuth_llmd`。

## `InferenceEvent`

`InferenceEvent` 是 LLM 和 runtime 之间的底层 transient stream 协议，不是 UI 事件，也不是 durable event。

所有 `InferenceEvent` 都有：

```python
type: str
generation_id: str
seq: int
run_id: str | None
```

不再有 `payload` 字段。具体事件用具体字段表达。

第一批 `InferenceEvent` 类型：

- `inference.generation.started`
- `inference.reasoning.delta`
- `inference.reasoning.completed`
- `inference.content.delta`
- `inference.tool_call.started`
- `inference.tool_call.delta`
- `inference.tool_call.completed`
- `inference.generation.completed`
- `inference.failed`
- `inference.aborted`

`ToolCallStarted` 表示模型开始构造一个 tool call，只携带 `index` 和可选 `id`，不可执行。

`ToolCallDelta` 表示 incomplete tool-call fragment，可观察、可累计，但不可转换成 `ToolIntent`，字段包括 `index`、可选 `id`、`name_delta`、`arguments_json_delta`、`raw`。

`ToolCallCompleted` 才携带完整 `ToolCall`，表示 runtime 可以把它转换为 `ToolIntent`。

`ReasoningCompleted` 是显式 stream boundary，不携带完整 reasoning 文本、摘要或耗时。它用于告诉 runtime observer / UI reasoning channel 已结束。

不引入 aggregate `CONTENT` / `REASONING` 事件。最终 canonical assistant message 只来自 `inference.generation.completed.message`。

`InferenceGenerationCompleted` 携带：

```python
message: InferenceMessage
finish_reason: str | None
usage: UsageInfo | None
```

不携带完整 provider raw response。

`InferenceFailed` 携带 `ErrorInfo`。`InferenceAborted` 携带 `reason: str`，因为 abort 是协作式控制流，不等同于 failure。

## `RuntimeEvent`

`RuntimeEvent` 是 runtime-level event language。它覆盖 model stream 的语义投影，但不一比一持久化所有 transient deltas。

`RuntimeEvent` 只用 `type` 表达事件身份。移除核心模型和存储 schema 里的 `namespace` 和 `name` 字段。分组只来自 dotted type 命名约定，例如 `model.completed`、`tool.completed`，不再是独立结构约束。

第一批 durable `RuntimeEventDraft` 类型：

- `run.created`
- `user.message`
- `model.started`
- `model.completed`
- `model.aborted`
- `model.failed`
- `tool.intent`
- `tool.proposed`
- `tool.started`
- `tool.completed`
- `approval.requested`
- `run.succeeded`
- `run.failed`
- `verification.failed`

第一批 transient model stream projection：

- `model.reasoning.delta`
- `model.reasoning.completed`
- `model.content.delta`
- `model.tool_call.started`
- `model.tool_call.delta`
- `model.tool_call.completed`

这些 transient projection 可以发给 live observer，但默认不进 `EventStore`。

durable tool workflow 从 `tool.intent` 开始。`model.tool_call.completed` 不作为恢复依据；恢复依据来自 durable `model.completed.message` 以及后续 `tool.intent` / `tool.proposed` / `tool.started` / `tool.completed`。

## Draft 与 Stored Event

`RuntimeEventDraft` 和完整 stored `RuntimeEvent` 分成两套类型。

调用方创建的是强类型 draft。`EventStore.append()` 负责补：

```python
id
run_id
seq
created_at
```

`seq` 只表示 durable EventStore timeline 中的顺序。transient runtime events 不拥有 store `seq`，也不假装属于 durable timeline。

`durability` 由事件 class 提供默认值。durable draft 进入 `EventStore.append()`；transient runtime events 直接发给 live sink / future EventBus。

类型层面区分 `DurableRuntimeEventDraft` 和 `TransientRuntimeEventDraft`。`EventStore.append()` 只接受 `DurableRuntimeEventDraft`，防止 transient model stream projection 被误写入 durable history。

## Runtime Live Sink

runtime 对外的 live sink 只发送 `RuntimeEvent`。

LLM 到 runtime 的内部流是：

```text
LLM -> InferenceEvent -> runtime
```

runtime 对外观察流是：

```text
runtime -> transient RuntimeEvent -> on_event / UI
runtime -> durable RuntimeEvent -> EventStore + on_event
```

因此 `run_agent_loop()` 的 `on_event` 不再混发 `InferenceEvent | RuntimeEvent`，而是只发强类型 `RuntimeEvent`。CLI/WebSocket 不应该直接依赖 LLM-level event shape。

## 字段约定

`run.created` 不再嵌入完整 `AgentRun`，只记录：

```python
query: str
metadata: dict[str, Any]
```

`model.started` 记录：

```python
turn: int
model: str
message_count: int
tool_count: int
```

`model.completed` 使用字段名 `message`，不再使用 `assistant_message`：

```python
turn: int
message: InferenceMessage
finish_reason: str | None
usage: UsageInfo | None
```

`tool.intent` 保存 `ToolIntent`，不保存原始 `ToolCall`。`ToolCall` 属于 LLM/runtime 边界；进入工具工作流后，runtime 语言是 `ToolIntent`。

`tool.completed` 保留为一个事件类型，但去掉 `denied: bool` 这类旁路标记，改用明确 outcome：

```python
intent: ToolIntent
result: ToolResult | None
message: InferenceMessage
outcome: Literal["succeeded", "failed", "denied"]
```

`message` 是 context reconstruction 使用的 canonical tool_result message。`result` 只在真实工具执行路径存在。

`approval.requested` 保存 runtime timeline 所需 snapshot，不嵌入完整 approval store model：

```python
approval_id: str
tool_call_id: str | None
title: str
reason: str
risk: str | None
```

approval status 和 resolved state 属于 approval store 当前状态，或未来单独的 `approval.resolved` event。

## 后果

这次重构是 breaking change。旧的 `from knuth_llmd import InferenceEvent, InferenceEventType`、`event.type == InferenceEventType.X`、`event.payload[...]`、`event.namespace/event.name` 都需要迁移。

SQLite `events` schema 需要移除 `namespace`、`name`、`payload_json` 的弱类型假设，或者至少让 `payload_json` 只作为 typed event serialization 的存储细节存在。

新的 SQLite `events` 表保留少量索引列，并用完整 typed JSON 保存 stored event：

```sql
events (
  id text primary key,
  run_id text not null,
  seq integer not null,
  type text not null,
  event_json text not null,
  created_at text not null,
  unique(run_id, seq)
)
```

`type` 是查询和索引列，canonical event body 存在 `event_json`。因为 `EventStore` 只保存 durable events，表里不需要 `durability` 列。

这次重构不兼容旧 SQLite event rows。实现应该在遇到旧 schema 时给出明确错误，提示这是 breaking event schema；开发期可以让用户删除旧 `~/.knuth/knuth.db` 或指定新数据库。不写旧 `namespace/name/payload_json` 到强类型事件的自动迁移，避免把弱类型历史重新拖回核心模型。

Context reconstruction、CLI renderer、runtime tests 和 store tests 都要迁移到强类型事件字段。

这个设计刻意让 `EventStore` 只保存 durable history，不承担 live EventBus 职责。transient runtime events 先通过 `on_event` 发送；未来如果引入 EventBus，再把这条路径抽出来。

## 考虑过的替代方案

### 保留旧 enum 和 re-export 兼容层

拒绝。重构目标是去除弱类型 payload 和不合理约束；兼容层会让新旧模型长期并存，并把错误推迟到运行时。

### 继续保留 `RuntimeEvent.namespace/name`

拒绝。它们没有表达独立领域概念，只是把 `type` 拆成两列。需要分组时，用 dotted `type` 命名约定或 helper，不把分组变成核心模型约束。

### 继续对外混发 `InferenceEvent | RuntimeEvent`

拒绝。`InferenceEvent` 是 LLM 和 runtime 之间的底层协议；外部观察者应该只消费 runtime 事件语言。runtime 负责把底层模型流投影成 transient `RuntimeEvent`。

### 把所有 model stream delta 持久化

拒绝。它会让 EventStore 快速膨胀，并把 token-level transient 信息误认为 durable recovery state。durable history 只保存 coarse runtime facts 和恢复所需 canonical messages。
