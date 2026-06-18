# 实现文档：独立 Artifact 存储 + 工具自有观测压缩

落地 [ADR-008（工具自有的观测压缩）](decisions/ADR-008-tool-owned-observation-condensation.md) 与 [ADR-009（独立的文件系统 artifact 存储）](decisions/ADR-009-standalone-filesystem-artifact-store.md)。本文给出文件级改动地图、分阶段步骤、删除清单与测试计划。

> **同步既有文档**：[knuth-v0-design.md](knuth-v0-design.md) §1.2 与事件目录里的 artifact 设计（ledger blob 侧店、`artifact_ref`、`observation_preview`、超阈值复水）已被 ADR-008/009 取代——已在该文就地加 superseded 注记，不重写历史正文。

## 1. 关键不变量（实现时必须守住）

- `ArtifactStore` 独立于 ledger（SQL / memory 无关），核心契约 **`(run_id, artifact_id)` → 本地文件系统路径**（经 per-run manifest 解析；id 单独不自足）。
- `Observation` 是**必填的最终模型可见文本**；artifact 是原文存档，**永不复水进 tape**。
- 自我压缩工具由**显式 `condensed` 标记**识别，中间件**绝对跳过**，无 per-result 兜底。
- 密钥脱敏发生在 **artifact 写入路径**（FS 写盘前）和**事件 append 前**（内联 observation）。
- "原文存进 store" 是 **L1 专属**；L2 未压缩 observation 内联进事件行。

## 2. 新增类型与接口

> **依赖方向（评审阻塞项）**：当前是 `knuth-runtime → knuth-toold → knuth-core`，`knuth-toold` **不能**反向依赖 runtime。因此工具可见的 `ArtifactSink` **和** broker 持有的 `ArtifactSinkProvider` 都放 `knuth-core`；`ToolBroker`（toold）只接收一个**可选 provider**；具体的 FS store（runtime）实现 provider，由 runtime 构造 broker 时注入。broker **不得**直接引用 store。

### 2.1 工具可见契约（置于 `knuth-core`）

工具通过 sink 写原文，sink 是挂在 `ToolRuntimeContext` 上的窄能力，run_id / tool_call_id 已绑定（与 `interrupt_signal` 同构）。broker 持有 provider，按 invocation 产出 sink。

```python
# knuth-core/src/knuth/core/artifacts.py（新增）
class StoredArtifact(KnuthModel):
    id: str
    path: str          # 具体本地文件路径，工具写进 observation 供 jq/grep
    kind: str
    sha256: str
    bytes: int

@runtime_checkable
class ArtifactSink(Protocol):
    async def put(self, content: str, *, kind: str, ext: str | None = None) -> StoredArtifact: ...

@runtime_checkable
class ArtifactSinkProvider(Protocol):
    def sink_for(self, run_id: str, tool_call_id: str) -> ArtifactSink: ...
```

- **只收 text**（ADR-009）。二进制留待将来的 `put_bytes`。
- `ext` 必须经**安全后缀校验**（白名单或正则，禁止路径分隔符 / `..`），防止逃逸出 `<root>/<run_id>/`。

### 2.2 `FilesystemArtifactStore`（实现 provider，置于 `knuth-runtime`）

```python
# knuth-runtime/src/knuth_runtime/artifacts.py（新增）
class FilesystemArtifactStore:                       # 实现 ArtifactSinkProvider
    def __init__(self, root: Path, *, redactor: RegexSecretRedactor, ttl_days: float = 7.0) -> None: ...
    def sink_for(self, run_id: str, tool_call_id: str) -> ArtifactSink: ...
    async def put(self, run_id: str, content: str, *, kind: str, ext: str | None = None) -> StoredArtifact: ...  # 写入即 pending
    async def mark_committed(self, run_id: str, artifact_ids: list[str]) -> None: ...  # 见下"提交协调"
    def path_for(self, run_id: str, artifact_id: str) -> Path: ...     # 经 manifest 解析（见下）
    async def read_text(self, run_id: str, artifact_id: str) -> str: ...  # 恢复 / 导出 / debug
    async def gc(self) -> None: ...                                   # 只删 pending 且过 TTL 的文件（崩溃孤儿）
    async def reclaim_run(self, run_id: str) -> None: ...              # 显式归档/删除时回收整个 run
```

- **提交协调（评审阻塞项）**：store **不查 ledger**，也无法自己判断某 artifact 是否被 committed 事件引用。改用 manifest 状态机：`put` 写文件 + `state=pending` 的 manifest 条目；runtime 在 `tool.invocation_completed` **成功 append 之后**调 `mark_committed(run_id, ids)` 把对应条目翻成 `committed`。`gc()` **只删 `pending` 且过 TTL** 的（即写了但完成事件从未提交的崩溃残留）；`committed` 只由 `reclaim_run` 删。store 仍与 ledger 解耦——commit 信号由 runtime 单向推入。

- **脱敏**：写盘前复用 `RegexSecretRedactor`（逻辑从 ledger 的 `_redact_artifact` 搬来）。
- **写入原子性**：写临时文件 → `fsync` → 原子 `rename`；artifact_id 唯一（content-addressed 或 uuid），避免碰撞与半文件。
- **id → 路径解析（评审阻塞项，ADR-009 承重点）**：每个 run 一份 `<root>/<run_id>/manifest.json`，记 `id → {rel_path, ext, sha256, bytes, state: pending|committed}`。`path_for` / `read_text` 取 `(run_id, artifact_id)` 经 manifest 解析，重启后从盘重载。事件层 `raw_artifacts` 存 id，调用方都在 run 上下文里，自带 run_id。模型读回用 observation 里烘焙的**具体路径**，不经 manifest。
- **GC（评审阻塞项，已重定）**：`committed` artifact **绝不自动回收**——`SUCCEEDED`/`INTERRUPTED` 可**永久** `continue_run`，任何 TTL/总量淘汰都会制造死路径。只在显式 `reclaim_run(run_id)`（归档/删除）时回收整个 run。`gc()` **只删 `pending` 且过 TTL**（见上"提交协调"）。被引用 artifact 的磁盘封顶留待"reclaimed 状态 + 死路径呈现 `[expired]`"的后续机制（ADR-009 后续/未决）。

### 2.3 类型字段变更

| 文件 | 类型 | 变更 |
|---|---|---|
| `knuth-core/.../tools.py` | `ToolResult` | 新增 `condensed: bool = False`；`artifacts: list[str]` 复活为"原文 artifact id 列表" |
| `knuth-core/.../tools.py` | `ToolResult.to_observation_text` | **（评审重要项）** 现在 `ERROR` 分支忽略 `content`、只回 `Tool error: ...`（[tools.py:27](../packages/knuth-core/src/knuth/core/tools.py)）。改为：`content` 非空时**优先返回 `content`**（成功/失败都成立），仅在 `content` 为空时回退错误消息。否则非零退出的 shell 会丢掉 `<process_output>` 与 artifact path。需验证无消费者依赖"ERROR 丢 content"的旧行为 |
| `knuth-core/.../runs.py` | `Artifact` | 保持 durable 字段（id/run_id/kind/sha256/created_at）；路径由 store 经 manifest 解析，不进 durable 模型 |
| `knuth-core/.../runtime_events.py` | `ToolInvocationCompletedDraft` | `observation` 语义改为必填最终可见文本；**删除 `observation_preview`**；`artifact_ref: str \| None` 改名/语义化为 `raw_artifacts: list[str]`（原文存档 id，永不复水）；**新增 `self_condensed: bool = False`** |

## 3. 文件级改动地图

### knuth-core
- [tools.py](../packages/knuth-core/src/knuth/core/tools.py)：`ToolResult` 加 `condensed`、用好 `artifacts`。
- [runtime_events.py:203](../packages/knuth-core/src/knuth/core/runtime_events.py)：`ToolInvocationCompletedDraft` 字段调整（见 §2.3）。
- `artifacts.py`（新增）：`ArtifactSink` / `ArtifactSinkProvider` / `StoredArtifact`（三者都要导出，别漏 provider）。

### knuth-runtime
- `artifacts.py`（新增）：`FilesystemArtifactStore`（§2.2）。
- [ledger.py](../packages/knuth-runtime/src/knuth_runtime/ledger.py)：
  - 移除 `put_artifact` / `get_artifact_text`（协议 149/152、Memory 1417/1429、Sql 1832/1857）与 `artifacts` 表（1525）；`_redact_artifact`（1164）逻辑搬进 store。
  - 不变量 [868](../packages/knuth-runtime/src/knuth_runtime/ledger.py) `observation is not None or artifact_ref is not None` → 改为 **`observation` 必填**。
  - **（评审重要项）** 事件字段是 breaking change：**bump `SQLITE_LEDGER_SCHEMA_VERSION` 2 → 3**（[178](../packages/knuth-runtime/src/knuth_runtime/ledger.py)），让 schema guard（1554-1565）对旧库**干净报错**（"breaking ledger schema"），而不是旧 `event_json` 在 parse 时炸出底层 Pydantic 错误；同步更新 guard 测试。
- [loop.py](../packages/knuth-runtime/src/knuth_runtime/loop.py)：
  - 重写 `_completion_for`（661-696）：删 offload，删 `OBSERVATION_INLINE_LIMIT`（136）/`_OBSERVATION_PREVIEW_CHARS`（137）；`observation = result.to_observation_text()` 直接落事件；从 `result.result.condensed` 设 `self_condensed`，从 `result.result.artifacts` 设 `raw_artifacts`。
  - **提交协调**：`tool.invocation_completed` 成功 append 之后，调 `artifact_store.mark_committed(run_id, raw_artifacts)`，把 `pending` 翻成 `committed`（在统一的 completion commit path 上，覆盖本地执行、外部 result submit、unknown resolve、denied backfill 等所有追加 completion 的路径）。
- [context.py](../packages/knuth-runtime/src/knuth_runtime/context.py)：
  - 删除复水（311-313）；`reconstruct_message_tape_from_events`（277）**去掉 `resolve_artifact_text` 参数**；tool-result 分支把 `event.self_condensed` 写进 `TapeMessage.metadata["self_condensed"]`，`raw_artifacts` 也带进 metadata 备查。
  - 调用点 263/273 不再传 resolver。
- [middleware.py](../packages/knuth-runtime/src/knuth_runtime/middleware.py)：
  - **（评审重要项）重命名 `ToolResultRedactionMiddleware` → `ObservationCondensationMiddleware`**（与 ADR-008 术语一致：`Redaction` 只留给密钥遮蔽）。开发期无包袱，**不留 alias**，直接改名。
  - `.process`（345）：跳过 `item.metadata.get("self_condensed")` 的条目；压缩逻辑抽成**可切换后端**（默认 headroom 简单版）。
  - `assert_checkpoint_complete`（385）：只对非 self_condensed 断言。
  - `MessageMiddlewareRunner`（145/162/184）：`reconstruct_*` 调用不再传 `ledger.get_artifact_text`。
- [__init__.py](../packages/knuth-runtime/src/knuth_runtime/__init__.py)：导出名同步改名（44、111），`ToolResultRedactionMiddleware` 不再导出。
- [agent.py](../packages/knuth-runtime/src/knuth_runtime/agent.py)：255/263 调用同样去掉 resolver 参数。
- 服务容器（`services`）：新增 `artifact_store`，与 `ledger` 并列构造、注入；构造 `ToolBroker` 时把 store 作为 `ArtifactSinkProvider` 注入。

### knuth-toold
- [base.py:45](../packages/knuth-toold/src/knuth_toold/base.py)：`ToolRuntimeContext` 新增 `artifacts: ArtifactSink | None = None`（窄能力、非 ledger，更新 docstring）。
- [broker.py:164](../packages/knuth-toold/src/knuth_toold/broker.py)：`ToolBroker` 持有**可选 `ArtifactSinkProvider`**（构造期注入，类型来自 `knuth-core`，**不引用 runtime store**）；构造 `ToolRuntimeContext` 时用 `provider.sink_for(run_id, tool_call_id)` 注入 `artifacts`，provider 为 None 时 `artifacts=None`。
- [builtins.py:175](../packages/knuth-toold/src/knuth_toold/builtins.py)：`ShellTool` 迁成 L1（见 §4）；`ctx.artifacts is None` 时**显式失败/降级**（见 §4），不静默吞掉。
- [process_output.py](../packages/knuth-toold/src/knuth_toold/process_output.py)：`<offload>` 字段内容从文件系统 `result_path` 改为 artifact `{id, path}`。

### knuth-agui / knuth-cli（评审阻塞项：consumer 迁移）

删 `observation_preview`、改 `artifact_ref`、给中间件改名会**立刻**打到这些 consumer，必须与字段变更**同切片**改（见 §7）：

- [translator.py:132](../packages/knuth-agui/src/knuth_agui/translator.py)：`content = event.observation or event.observation_preview or ""` → 去掉 `observation_preview` 回退（`observation` 已必填）。
- [render.py:248](../packages/knuth-cli/src/knuth_cli/render.py)：同样去掉 `observation_preview` 回退；且 shell 分支 `parse_tagged_process_output` 解析的 `<offload>` 形状已变（`result_path` → artifact `{id, path}`），`_print_shell_completed` 的渲染需同步适配新形状。
- [prompts.py:11,38](../packages/knuth-cli/src/knuth_cli/prompts.py)：**（评审阻塞项）** CLI/IM runtime factory 直接 import（11）并实例化（38）旧名 `ToolResultRedactionMiddleware`，随改名同步为 `ObservationCondensationMiddleware`，否则 runtime factory 启动即断。

## 4. shell 迁移成 L1（v1 唯一迁移的工具）

- 删除 `_offload_root` / `~/.knuth/offload` 全套（`_build_offload_payload` 的文件写入、`_file_metadata`、`result.json`）——**不兼容、直接弃用**（ADR-009）。
- 超阈值时改为：`stdout_art = await ctx.artifacts.put(stdout, kind="shell_stdout", ext=".txt")`，stderr 同理。
- 返回 `ToolResult(content=render_tagged_process_output(stdout=preview, stderr=preview, return_code, offload={"status":"offloaded","stdout":{...,"path":stdout_art.path}, ...}), artifacts=[stdout_art.id, stderr_art.id], condensed=True)`。
- 精简版里写**具体 path**，提示模型用 `jq`/`grep`/`find` 查原文。
- `condensed=True` 仅在成功/失败的常规结果上；`interrupted` 路径（341-357）不带标记，维持现状。
- **失败路径不丢结构（依赖 §2.3 的 `to_observation_text` 修复）**：非零退出仍返回 `status=ERROR` 的 `ToolResult`，但 `content` 带着 `<process_output>` + path；修复后 `to_observation_text` 在 `content` 非空时返回 `content`，结构化 observation 与 path 才会进模型可见文本。（若不改 `to_observation_text`，shell 失败需显式返回带 `observation` 的 `ToolExecutionResult`。）
- **`ctx.artifacts is None`**（未注入 provider，如某些测试/嵌入场景）：shell **不静默回退到内联大输出**——要么显式报工具错误（"artifact sink unavailable"），要么按未压缩 L2 结果返回（不带 `condensed`），交由中间件兜底。二选一在实现时定，但必须是**明确**行为并有测试覆盖。

## 5. 删除清单

- `loop.py`：`_completion_for` 的 offload 分支、`OBSERVATION_INLINE_LIMIT`、`_OBSERVATION_PREVIEW_CHARS`。
- `context.py`：tool-result 复水（311-313）、`resolve_artifact_text` 参数链路。
- `ledger.py`：`put_artifact` / `get_artifact_text` / `artifacts` 表 / `_redact_artifact`（搬走）。
- `runtime_events.py`：`observation_preview` 字段。
- `builtins.py`：`_offload_root` / `_build_offload_payload` 文件写入 / `_file_metadata`。
- `middleware.py` + `__init__.py`：旧名 `ToolResultRedactionMiddleware`（改名为 `ObservationCondensationMiddleware`，不留 alias）。
- consumer 里的 `observation_preview` 回退分支（`translator.py`、`render.py`）。

## 6. 测试计划

- **ArtifactStore**：put→path 可读 + 条目为 `pending`、sha256 一致、写入即脱敏（密钥被 mask）、`mark_committed` 翻成 `committed`、`gc()` **只删 pending+过 TTL** 而 `committed` 不删（即便 run 长期 inactive）、`reclaim_run` 删除整个 run、重启后 `path_for(run_id, id)` / `read_text` 经 manifest 仍解析。
- **L1 端到端**：shell 大输出 → 原文进 store、observation 是精简版且含 path、事件 `self_condensed=True`、中间件**未改写**该条；模型可对 path `jq`（用真实 shell）。
- **回归（修复的 bug）**：被卸载的 shell 结构化输出**不再被中间件切坏**（`<process_output>` 完整）。
- **L2 兜底**：未标记工具的大 observation 仍被后端压缩成 ReplacePatch；内联原文经过 append 前脱敏。
- **语义反转**：`observation` 必填不变量；无复水路径（reconstruct 不依赖 artifact）。
- **中间件**：`self_condensed` 条目被 `process` 与 `assert_checkpoint_complete` 同时豁免。
- **shell 失败结果**：非零退出的 observation 含 `<process_output>` + path（`to_observation_text` 修复后）。
- **consumer（评审补充）**：CLI `render.py` 渲染（含 shell 新 `<offload>` 形状）、AGUI `translator.py` 不再依赖 `observation_preview`。
- **SQLite schema guard（评审补充）**：旧版本库被干净拒绝（version 2 → 3），不在 parse 期炸 Pydantic。
- **重启稳定性（评审补充）**：进程重启后 `path_for(run_id, id)` / `read_text` 仍能解析（manifest 重载）。
- **`ctx.artifacts is None`（评审补充）**：shell 走明确的失败/降级行为，不静默内联。
- **存储健壮性（评审补充）**：`ext` 安全后缀校验拒绝路径分隔符 / `..`；并发/重复 put 不产生半文件或碰撞（原子写）。

## 7. 阶段与风险

| 阶段 | 内容 | 可独立合入 |
|---|---|---|
| P0 | `ArtifactStore`（含 manifest / 原子写 / ext 校验 / GC / 脱敏）+ 配置 | **是**（纯新增，未接线） |
| P1 | **事件/类型切片（不可拆）**：`ToolInvocationCompletedDraft` 字段变更 + `ToolResult.condensed/to_observation_text` + `context.py` 重建（去复水、写 metadata）+ `ledger` schema bump 与不变量 + **同切片的 consumer**（`translator.py`、`render.py`、CLI `prompts.py` runtime factory）+ 中间件改名/跳过逻辑 | 是，但**必须整片一起合** |
| P2 | 接线：services 注入 `artifact_store`；broker 持 `ArtifactSinkProvider`；`ToolRuntimeContext.artifacts` | 依赖 P0+P1 |
| P3 | `loop._completion_for` 简化 + `ledger` artifact API 移除 | 依赖 P2 |
| P4 | shell 迁 L1 + 删旧 offload + CLI shell 渲染适配新 `<offload>` | 依赖 P2 |
| P5 | 测试补齐 + 删除清单收尾 | 最后 |

> **（评审阻塞项）P1 不能再宣称"核心类型可单独合入"**：删 `observation_preview` / 改 `artifact_ref` 会**立刻**打到 `context.py` 重建、CLI [render.py:248](../packages/knuth-cli/src/knuth_cli/render.py)、AGUI [translator.py:132](../packages/knuth-agui/src/knuth_agui/translator.py)。这些 consumer 与字段变更必须**同一不可拆切片**，否则中途任一提交都会让 CLI/AGUI 读到不存在的字段。

**事件字段兼容**：变更会让旧持久化事件无法通过新的强类型 union（[ADR-002](decisions/ADR-002-strongly-typed-events.md)）。**决定：开发期无旧 run 包袱，直接重建 ledger，不写迁移/容忍层**；通过 P1 里的 `SQLITE_LEDGER_SCHEMA_VERSION` 2→3 让旧库被 guard 干净拒绝。
