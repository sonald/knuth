# ADR-009: 独立的文件系统 artifact 存储

## 状态
Accepted

## 日期
2026-06-18

## 背景

ADR-008 的 L1 让工具返回精简版 observation、把原文存档。但精简版**经常不够**：一个超大 JSON 压成小版本后，模型要细节就得对原文跑 `grep` / `jq` / `find`——这正是压缩的目的，**逼模型用工具查必要片段，而不是把整坨数据塞回上下文**。

这要求原文是一个**可被 shell 工具操作的真实文件**。而今天的 artifact 存储是 ledger 的"side store"（SQL `artifacts` 表的 blob，或 memory dict），**不是文件系统路径**——模型没法 `jq` 一个 SQLite blob。

shell 今天已经把大输出写到 `~/.knuth/offload/shell/<run_id>/<tool_call_id>/`（真实文件、可 grep），这个"文件系统卸载"的直觉是对的，但它 ad-hoc、绕过了密钥遮蔽、也不在 durable run history 里。

## 决策

把 artifact 存储从 ledger 里**抽出来，做成独立的、与 runtime 无关的 `ArtifactStore`**：

- **文件系统后端**（今天）。artifact 是 `<artifact_root>/<run_id>/<artifact_id>[.ext]` 的真实文件。将来可由"通用存储协议"换成远程后端。
- **承重不变量：`ArtifactStore` 必须能把 `(run_id, artifact_id)` 解析成一个本地文件系统路径。** FS 后端路径即存储；未来远程后端访问前物化到本地缓存。否则 `grep`/`jq`/`find` 立刻失效——这是整个抽象的承重点，任何后端都得满足。**不是 id-only**：单凭 id 反推不出 run 与 ext（见下条）。
- **id → 路径的重启稳定解析**：文件落在 `<root>/<run_id>/<artifact_id><ext>`，仅凭 id 反推不出 run_id 与 ext，事件又只存 id。因此 `ArtifactStore` 维护一份 **durable 索引**——每个 run 一份 `<root>/<run_id>/manifest.json`，记录 `id → {rel_path, ext, sha256, bytes}`。解析接口取 `(run_id, artifact_id)`（runtime 侧的恢复/导出/GC 调用都在 run 上下文里，天然有 run_id），经 manifest 查路径；进程重启后从磁盘重载 manifest，保证稳定。模型可见 observation 里烘焙的是**具体路径**，读回不依赖 manifest；manifest 只服务 runtime 侧。
- **密钥遮蔽搬到存储的写入路径**：greppable 的原文仍然脱敏。
- **ledger 只按 id 引用 artifact**，不再持有 bytes。无论 ledger 是 SQL 还是 memory，artifact 存储都独立于它。
- 工具通过挂在 `ToolRuntimeContext` 上的**窄能力句柄**（`ArtifactSink`）写入，拿到 artifact（含具体本地路径），把**路径直接写进精简版 observation** 供模型查询。这保持了"工具不直接碰 ledger / RunSession"的原则——句柄不是 `RunLedger` 本体，与已有的 `interrupt_signal` 同构。

这**取代了** ADR-008 设计过程中一度选定的"ledger 支撑的 sink"：sink 改指向独立 `ArtifactStore`。支持它的两个理由（durability、密钥遮蔽）依然成立，只是搬家。

### 模型可达的引用

模型读回走**通用 shell 工具**（`jq`/`grep`/`find`）操作那个具体路径，**不需要专门的 `read_artifact` 工具**。因为 Knuth 是**本地 agent 运行时**，同机器路径稳定，所以精简版 observation 里直接写具体本地路径（它本身就是 durable 引用）。跨机器移植在 v1 不在范围内；将来远程后端落地时再加"物化 + 路径解析"层（那时才需要抽象句柄）。这与 [ADR-005（工具不做路径围栏）](ADR-005-no-path-confinement-in-tools.md)一致——模型本就能读任意路径。

### ArtifactSink 契约

- **只收 text**：`ctx.artifacts.put(content: str, *, kind: str, ext: str | None = None) -> StoredArtifact`。动机场景（`jq`/`grep`/`find`）全是文本，shell 也是文本，密钥脱敏（基于文本）得以统一生效。二进制存档是将来需求，届时再加明确"跳过 regex 脱敏、且不可 grep"的 `put_bytes`，不预付复杂度。
- 返回 **`StoredArtifact`**（工具句柄，**新类型**），**同时带 `id`（durable 句柄）和 `path`（具体本地路径）**：工具用 `path` 写进 observation，事件存 `id`。core 既有的 `Artifact` 保持纯 **durable metadata**（id/run_id/kind/sha256/created_at），**不加 `path`**——`Artifact` 是元数据，`StoredArtifact` 是工具拿到的句柄，两者分名。
- 一个结果可产出**多个 artifact**（`ToolResult.artifacts: list[str]` 存 id 列表）：比如 shell 把 stdout、stderr 分成两个或合成一个，由工具自定。
- 带 `kind` + 扩展名（如 `kind="shell_stdout"`, `ext=".txt"`），让落地文件有有用后缀，模型一眼知道该 `jq` 还是 `grep`。

### 生命周期 / GC

- **被引用的 artifact 只能显式回收。** `SUCCEEDED` / `INTERRUPTED` 的 run 可被 `continue_run` **永久**继续（[session.py](../../packages/knuth-runtime/src/knuth_runtime/session.py) 发 `RunResumedDraft`，继续能力不随时间过期），历史 observation 里烘焙的路径必须一直有效。因此**任何基于时间 / 总量的自动淘汰都会制造死路径**——被 committed 事件引用的 artifact **绝不自动回收**，只在 run 被**显式归档 / 删除**时经 `reclaim_run(run_id)` 回收。
- **只有孤儿可自动回收。** store 不查 ledger，用 manifest 状态机区分 committed 与否：`put` 写入即 `pending`，runtime 在 `tool.invocation_completed` append 成功后调 `mark_committed` 翻成 `committed`。后台 `gc()` **只删 `pending` 且过 TTL** 的文件（写了但完成事件从未提交的崩溃残留）——这是 v1 **唯一**的自动回收；`committed` 只由 `reclaim_run` 删。commit 信号由 runtime 单向推入，store 仍与 ledger 解耦。
- 代价：用户从不归档的 run，其 artifact 会一直占盘——这是"留住你可能继续的东西"的正确行为。要给**被引用** artifact 也封顶磁盘，需要引入 **artifact-reclaimed 状态 + 续聊时把死路径一次性 durable 改写为 `[expired]`** 的机制；v1 不做，列入后续。

## 迁移

shell 现有的 `~/.knuth/offload/shell/...` 私有卸载**不做兼容、直接弃用**：shell 迁到 `ArtifactStore` 后删除 `_offload_root` 及相关代码；旧路径下遗留的文件不读取、不迁移，由用户或常规清理处理。

## 影响

- shell 的私有文件卸载收编进 `ArtifactStore`，顺带获得密钥遮蔽和 run history 归属。
- artifact 成为 run history 的一等公民（可导出、可恢复）。
- ledger 与大 blob 存储解耦，`put_artifact` / `get_artifact_text` 从 ledger 接口迁出。

## 后续 / 未决

- **被引用 artifact 的磁盘封顶**：v1 只显式 `reclaim_run` + 孤儿 `gc()`，被引用 artifact 不自动淘汰。要做自动封顶需引入 artifact-reclaimed 状态 + 续聊时死路径呈现为 `[expired]`（一次性 durable 改写），属后续工作。
- 其余实现细节见 [实现文档](../artifact-store-and-condensation-implementation.md)。
