# ADR-008: 工具自有的观测压缩

## 状态
Accepted

## 日期
2026-06-18

## 背景

一个大的 tool result 今天要经过三层处理：

1. **执行层卸载**（`loop.py` 的 `_completion_for`）：observation 超过 `OBSERVATION_INLINE_LIMIT`（8KB）就 `put_artifact` 存进 ledger artifact，事件只留 `artifact_ref` + 512 字符 `observation_preview`。
2. **tape 重建层**（`context.py`）：重建 model-visible 消息时，又用 `get_artifact_text` 把完整原文从 artifact 复水回 tool_result 消息。
3. **中间件压缩层**（`ToolResultRedactionMiddleware`）：复水后内容超过 `max_chars`（4096）就替换成 `"Result redacted... excerpt:"` + 前 1200 字符。

这套设计有两个问题：

- **通用压缩对不同工具一刀切。** 不同工具对"怎么压才不丢关键信息"的判断完全不同（shell 要保留 exit code + 尾部，read_file 要保留结构，超大 JSON 要保持可被 `jq` 查询）。运行时不可能比工具自己更懂它的数据形状。
- **通用截断会切坏结构化输出。** shell 已经在自己产出 `<process_output>` 结构化精简输出，但一个被卸载的 shell 结果渲染后约 >4096 字符，于是中间件介入、从 `<stdout>` 中间切断、丢掉 `</stdout>...<offload>...</process_output>` 尾部——结构和卸载指针全毁。shell 今天只靠"输出恰好够小"才侥幸躲过。

"Redaction" 一词还被重载：`RegexSecretRedactor` 做的是**密钥遮蔽**（安全、append 前、不可逆），`ToolResultRedactionMiddleware` 做的是**按体积裁剪**（上下文余量）。两者是完全不同的关注点。

## 决策

把工具结果压缩从"运行时通用一刀切"改成**两层互补**。

### L1：工具自我压缩

工具拿到一个 artifact 写入能力（见 ADR-009），自己把完整输出存进 `ArtifactStore`，返回一个**已经精简好的 `Observation`**，并在返回结果上带一个**显式标记** `condensed`。运行时把这个标记带进 `tool.invocation_completed` 事件，再在 tape 重建时写进 `TapeMessage.metadata["self_condensed"]`。`ObservationCondensationMiddleware`（由 `ToolResultRedactionMiddleware` 改名，见"术语"）**绝对跳过**被标记的条目——完全信任工具对自身数据的压缩判断，不设 per-result 兜底。全局上下文压力是将来完整版 `ContextCompactionMiddleware` 的职责。

检测信号用**显式标记**而非"是否存在 artifact 引用"来推断：工具可能存了原文但仍愿意被中间件再压，也可能自我精简了却没有原文可存——这两件事必须解耦。

标记落在 **`ToolResult.condensed`**（描述的是"结果内容已是精简最终版"，本属 `ToolResult` 语义）。`ToolExecutionResult.succeeded/failed(result)` 通过 `.result` 自然带上它；`interrupted` / `unknown` 这两种 outcome 的 observation 是运行时撰写的（用户停止提示、恢复占位），本就不是工具自我压缩的产物，无需此标记。链路：`ToolResult.condensed` → `_completion_for` 写进 `ToolInvocationCompletedDraft.self_condensed` → 重建拷进 `TapeMessage.metadata["self_condensed"]` → 中间件据此跳过。

### L2：中间件兜底

`ObservationCondensationMiddleware` 退化成**可切换后端**（headroom / 其它 / 简单实现）的兜底，**只处理没有 `self_condensed` 标记的** observation。它不再自己发明唯一的压缩算法。

**"原文存进 `ArtifactStore`"是 L1 专属**：只有自我压缩的工具往 store 存原文。L2 工具直接内联完整 observation，中间件压模型可见版，原文留在事件行里（append 前仍过密钥脱敏，所以只是事件行变大、非安全问题；SQLite 扛得住）。这忠于"删掉 `_completion_for`"的决定，也保持主线干净：能产生大输出的工具就该做成 L1，L2 偶尔吐大 blob 恰是"该迁 L1"的可见信号，而非用运行时卸载长期掩盖。

### `artifact_ref` 语义反转

引入 L1 后，`tool.invocation_completed` 的字段语义改变。`artifact_ref` **字段本身**确实只有这条流水线引用（`runtime_events.py` 定义、`loop.py` 写入、`context.py` 复水、`ledger.py` 一处不变量），可安全重定义；**但同批要删的 `observation_preview` 另有外部消费者**——CLI [render.py](../../packages/knuth-cli/src/knuth_cli/render.py) 与 AGUI [translator.py](../../packages/knuth-agui/src/knuth_agui/translator.py) 都读它。因此整个事件 shape 变更**并非无消费者**，必须与这两个 consumer **同一不可拆切片**迁移（见[实现文档](../artifact-store-and-condensation-implementation.md) §7）。字段变更：

- `observation`：从"可选、大则为 None"升级为**必填的最终模型可见文本**。
- 原文存档：复活今天无人消费的 `ToolResult.artifacts`（或重定义 `artifact_ref`）承载存档指针，**永不复水进 tape**。
- `observation_preview`：删除。
- `loop.py` 的 `_completion_for` 卸载、`OBSERVATION_INLINE_LIMIT`、`context.py` 的复水：全部删除。
- `ledger.py` 不变量 `observation is not None or artifact_ref is not None` 改为 **`observation` 必填**。

### 术语

"Redaction" 只保留给**密钥遮蔽**；按体积裁剪 observation 一律叫 **ObservationCondensation**（见 CONTEXT.md）。对应地，中间件 `ToolResultRedactionMiddleware` **改名为 `ObservationCondensationMiddleware`**（开发期无包袱，不留 alias；本文背景段沿用旧名描述改造前的现状）。

## 影响

- shell 从 ad-hoc 的 `<process_output>` + 私有文件卸载，迁移到统一的 L1 模式（存储侧见 ADR-009）。
- 通用截断切坏结构化输出的 bug 被根除：被标记的工具直接跳过。
- 未标记的工具仍由 L2 兜底，行为向后兼容。

### 迁移范围

**v1 只迁 shell 成 L1**（它已有全套 offload 机制，是最自然的第一个，也正是被通用截断切坏结构的那个）。read_file、search 等留待后续——read_file 尤其值得单独设计（"读大文件 → 返回结构 + 头部 + artifact 路径"很可能也该做成 L1，但会牵动 read 工具本身的行为定义，不并入本次）。

## 未决问题

- 无（细节均已在 ADR-009 收口或列入其未决项）。
