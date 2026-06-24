# 架构变更决策：MessageProjectionCheckpoint 作为 message projection 读取缓存

状态：Accepted
日期：2026-06-23
相关模块：`MessageTape`、`ContextBuilder`、`MessageMiddlewareRunner`、`RunLedger`、`ProjectionCheckpointWriter`

## 背景

Knuth 的模型上下文读取当前会从 run 的完整 durable event history 重建 `MessageTape`。长 run 下，这让 `ContextBuilder` 和 `MessageMiddlewareRunner` 在每次请求或 middleware checkpoint 前重复从头 fold 历史。`docs/knuth-v0-design.md` 曾预留 `run.checkpoint`，但当前要解决的问题更窄：只优化 model-visible message projection 的重建成本。

## 决策

新增 durable event `message.projection_checkpoint`，领域名为 `MessageProjectionCheckpoint`。它保存某个 `through_seq` 之前已经全量 fold 得到的 model-visible `TapeMessage` projection；conversation loader 选择最新可用 checkpoint 作为初始 tape，再 fold `through_seq` 之后的普通 message projection events。

该事件不是 runtime snapshot，不是 `DecisionEvent`，不参与 run/tool/approval reducers，也不是 `MessageMiddleware` 产物。写入由 runtime 内部的 `ProjectionCheckpointWriter` 在完整 turn closed 后触发；writer 不生成 patches，不改变模型可见消息序列，只追加 cache fact。

第一版本的 payload 只包含：

```python
type = "message.projection_checkpoint"
through_seq: int
messages: list[CheckpointTapeMessage]
```

其中 `CheckpointTapeMessage` 保存 stable message `id`、完整 `InferenceMessage`、`TapeItemSource` 和 semantic metadata。payload 不保存 system preamble、tools、`ContextSnapshot`、suppressed historical messages、version 或 digest。

## 约束

- `ProjectionCheckpointWriter` 只在 provider-valid closed conversation boundary 写 checkpoint；不 checkpoint `WAITING_APPROVAL`、`WAITING_TOOL_RESULT`、open tool batch 或 recovery waiting state。
- 写入 checkpoint 仍走 `RunLedger.apply()`；reducer 对该事件 no-op，但 ledger 校验 `through_seq == current run.last_seq`，从而保证同步第一版本中 stored checkpoint 紧跟覆盖边界。
- writer 生成 checkpoint 时从原始 message projection events 全量 fold 到 `through_seq`，不基于旧 checkpoint 再缓存。
- loader 和 middleware runner 可通过共同底层函数使用 checkpoint fast path；`RunLedger` 写入校验和 rewrite target validation 继续从原始事件 fold，不依赖 checkpoint。
- tail fold 和 full replay 都忽略 `message.projection_checkpoint`。完整 replay 忽略所有 checkpoint events 后，必须与 checkpoint fast path 得到相同 model-visible messages。
- checkpoint 解码失败或 `through_seq` 不合法时，loader 倒序尝试更早 checkpoint，最终回退全量 fold；坏 checkpoint 只造成性能退化，不让 run 不可读。
- 第一版本保留所有 checkpoint events，不做 retention，不新增 partial index。若 lookup 成为可测热点，再单独增加索引。

## 被拒绝的方案

### 独立物化表

拒绝。物化表会引入 ledger append 与 projection upsert 的一致性问题，并需要定义覆盖、删除和迁移语义。当前只需要一个 append-only cache fact。

### 裸 `InferenceMessage[]` checkpoint

拒绝。`MessageMiddleware` 的 patch target 依赖 stable `TapeMessage.id`、`origin` 和 metadata。只保存 provider messages 会丢失后续 insert/replace 所需的 projection identity。

### 把 checkpoint 做成 `MessageMiddleware`

拒绝。middleware 是语义变更路径，会追加 rewrite/insert projection events；checkpoint writer 是维护型缓存写入，不应获得 patch、priority、target 或 middleware identity 语义。

### 加 version、digest 或 partial index

拒绝作为第一版本内容。当前尚无发布兼容承诺，不为未来 projection schema 分支预留 version；checkpoint 不是审计证明，不需要 digest；partial index 等性能数据证明需要时再加。

## 后果

`ContextBuilder` 和 `MessageMiddlewareRunner` 应改用共同的 message tape load 函数读取 projection。`RunLedger` 协议需要新增面向 message projection 的窄读 API：读取最新可用 checkpoint，以及读取排除 checkpoint events 的 projection tail events。raw event timeline 和 live durable observation 可以展示 checkpoint event，但 model context 与 rewrite audit 必须忽略它。
