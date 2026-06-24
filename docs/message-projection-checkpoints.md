# Message Projection Checkpoints

状态：第一版本设计
日期：2026-06-23

本文定义 Knuth 用于加速长 run message projection 重建的第一版本方案。核心目标是减少重复 full fold，不引入独立 snapshot 系统、物化表、checkpoint retention、projection versioning 或写入校验 fast path。

## 核心决策

在 append-only ledger 中增加 durable runtime event `message.projection_checkpoint`。它保存一个 `MessageProjectionCheckpoint`：某个 `through_seq` 之前已经 fold 完成的 model-visible `MessageTape` projection。

加载 conversation 时，runtime 读取最新可用 checkpoint，再 fold 其后的普通 message projection events。完整原始事件仍然保留；checkpoint 只优化读取，不改变 run 语义。

## 领域边界

`MessageProjectionCheckpoint` 是 projection cache fact，不是 `DecisionEvent`。它不改变：

- `Run.status`
- tool invocation state
- approval state
- model-visible message semantics
- rewrite audit

`ProjectionCheckpointWriter` 是 runtime maintenance component，不是 `MessageMiddleware`。它不实现 `process(ctx, messages) -> patches`，不生成 `message.rewrite_*`，也不参与 middleware priority ordering。

## Event shape

第一版本的 draft 形状：

```python
class MessageProjectionCheckpointDraft(RuntimeEventDraftBase):
    type: Literal["message.projection_checkpoint"] = "message.projection_checkpoint"
    through_seq: int
    messages: list[CheckpointTapeMessage]


class CheckpointTapeMessage(KnuthModel):
    id: str
    message: InferenceMessage
    origin: TapeItemSource
    metadata: dict[str, Any] = Field(default_factory=dict)
```

通用 stored event envelope 继续提供 `id`、`run_id`、`seq`、`created_at` 和 `type`。payload 不重复保存这些字段。

不保存：

- system preamble
- visible tools
- `ContextSnapshot`
- suppressed historical messages / `TapeAnchor`
- projection version
- digest / checksum

## Write path

`ProjectionCheckpointWriter` 只在完整 turn closed 后运行，推荐顺序：

```text
assistant turn closed
-> MessageMiddlewareRunner.run_checkpoint(AFTER_TURN_CLOSED)
-> ProjectionCheckpointWriter.maybe_append(run_id)
-> run.succeeded or next wait boundary
```

第一版本不在 `BEFORE_MODEL_REQUEST` 常规写 checkpoint。

写入步骤：

```text
1. 读取当前 run.last_seq，记为 through_seq。
2. 从原始 message projection events 全量 fold 到 through_seq。
3. 序列化 tape.model_visible() 为 CheckpointTapeMessage[]。
4. 通过 RunLedger.apply(run_id, MessageProjectionCheckpointDraft(...)) 追加事件。
5. Ledger 校验 draft.through_seq == current run.last_seq。
```

如果第 2 步和第 4 步之间有别的 durable event 插入，校验失败；writer 跳过本次 checkpoint，下一次安全边界再试。

## Policy

第一版本策略只使用简单阈值：

```python
class ProjectionCheckpointPolicy:
    min_events_since_checkpoint: int = 200
    min_messages: int = 8
```

写入条件：

```text
run.last_seq - latest_checkpoint.through_seq >= min_events_since_checkpoint
and len(tape.model_visible()) >= min_messages
```

不按 turn 数、token 数、payload byte size 或内容 digest 去重。即使 message payload 与上一 checkpoint 相同，只要覆盖边界推进且策略满足，也允许写入。

## Read path

读取 model-visible message tape 的共同底层函数：

```python
async def load_message_tape(
    ledger: RunLedger,
    run_id: str,
) -> MessageTape:
    checkpoint = await ledger.latest_message_projection_checkpoint(run_id)
    if checkpoint is None:
        events = await ledger.list_message_projection_events(run_id)
        return await reconstruct_message_tape_from_events(events)

    initial = MessageTape(
        items=[
            TapeMessage(
                id=item.id,
                message=item.message,
                origin=item.origin,
                metadata=dict(item.metadata),
            )
            for item in checkpoint.messages
        ]
    )
    events = await ledger.list_message_projection_events(
        run_id,
        after_seq=checkpoint.through_seq,
    )
    return await fold_message_tape(initial, events)
```

Writer 使用无 checkpoint 的固定边界 fold：

```python
async def load_message_tape_without_checkpoint(
    ledger: RunLedger,
    run_id: str,
    *,
    through_seq: int,
) -> MessageTape:
    events = await ledger.list_message_projection_events(
        run_id,
        through_seq=through_seq,
    )
    return await reconstruct_message_tape_from_events(events)
```

`ContextBuilder._assemble()` 和 `MessageMiddlewareRunner` 都应调用 `load_message_tape(...)`，而不是直接 `ledger.list_events(...)` + full reconstruct。

## Fold helpers

保留全量入口，但拆出增量实现：

```python
async def fold_message_tape(
    initial: MessageTape,
    events: list[RuntimeEvent],
) -> MessageTape:
    ...


async def reconstruct_message_tape_from_events(
    events: list[RuntimeEvent],
) -> MessageTape:
    return await fold_message_tape(MessageTape(items=[]), events)
```

`fold_message_tape(...)` 必须忽略 `message.projection_checkpoint`。checkpoint 只参与 loader 选择基线，不参与 tail fold。

## RunLedger API

`RunLedger` 协议增加窄读 API：

```python
@dataclass(frozen=True)
class MessageProjectionCheckpointRecord:
    seq: int
    through_seq: int
    messages: tuple[CheckpointTapeMessage, ...]


class RunLedger(Protocol):
    async def latest_message_projection_checkpoint(
        self,
        run_id: str,
    ) -> MessageProjectionCheckpointRecord | None:
        ...

    async def list_message_projection_events(
        self,
        run_id: str,
        *,
        after_seq: int | None = None,
        through_seq: int | None = None,
    ) -> list[StoredRuntimeEvent]:
        ...
```

`list_message_projection_events(...)` 永远排除 `message.projection_checkpoint`。SQLite 实现应在 SQL 层过滤：

```sql
SELECT event_json
FROM events
WHERE run_id = ?
  AND seq > ?
  AND type != 'message.projection_checkpoint'
ORDER BY seq ASC;
```

第一版本不新增 partial index。最新 checkpoint 查询先依赖现有 `(run_id, seq)` 唯一索引的倒序扫描和 type 过滤。

## Failure behavior

Checkpoint corruption is non-fatal.

读取 checkpoint 时：

```text
1. 按 seq 倒序尝试 checkpoint candidates。
2. JSON 解码失败、schema validation 失败、through_seq 不合法时跳过。
3. 找到最新可用 checkpoint 就使用。
4. 没有可用 checkpoint 时回退 full replay。
```

坏 checkpoint 不写 durable failure event；第一版本只记录 diagnostics / debug log。

Writer 失败不影响当前 run 结果：

- policy 判断失败：跳过
- append 时 `through_seq != current last_seq`：跳过
- transient storage failure：记录诊断，run 继续

但如果 full projection fold 本身发现 ledger 语义损坏，这不是 checkpoint 失败，不能被 writer 掩盖。

## Visibility

`message.projection_checkpoint` 是 durable event，因此 raw event timeline、debug route、live durable observation 可以看到它。UI 默认只应摘要显示：

```text
message.projection_checkpoint through_seq=1234 messages=87
```

它不进入：

- model context messages
- rewrite audit
- run/tool/approval reducers

## Tests

第一版本测试至少覆盖：

- no checkpoint 时，`load_message_tape` 与 full replay 一致。
- 一个 checkpoint + tail events 时，fast path 与 full replay 一致。
- 多个 checkpoint 时使用最新可用 checkpoint。
- tail fold 忽略后续 checkpoint events。
- checkpoint payload 只保存 model-visible `TapeMessage`，不保存 `TapeAnchor`。
- corrupt latest SQLite checkpoint row 时回退更早 checkpoint 或 full replay。
- `list_message_projection_events` 在 SQL 层排除 checkpoint event。
- writer 写入时校验 `through_seq == current last_seq`。
- writer 只在 closed turn boundary 运行，不 checkpoint waiting approval / waiting tool result / open tool batch。
- `ContextBuilder` 和 `MessageMiddlewareRunner` 使用共同 `load_message_tape`。
- `RunLedger.apply_many` rewrite validation 不依赖 checkpoint fast path。

## Explicit non-goals

- 不定义摘要或压缩算法。
- 不引入独立 snapshot table。
- 不做 checkpoint retention / deletion。
- 不做 projection version compatibility。
- 不做 digest / checksum proof。
- 不在第一版本添加 partial index。
- 不让 checkpoint 参与 write validation 或 aggregate state fold。
