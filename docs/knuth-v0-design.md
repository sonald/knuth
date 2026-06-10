# Knuth v0 设计（修订版）

> 本文档取代 `knuth v0 方案.md`，综合了原方案、两轮评审意见（架构评审 + 同行评审）以及
> "事件为唯一权威"的最终方向讨论。CONTEXT.md 中已定义的语言（RuntimeEvent、AgentLoop、
> ToolBroker、BlockingHook、SystemSectionProvider 等）继续有效，本文只新增和修订。

## 0. 目标重述

v0 的目标不是"先做一个能跑的 agent loop"，而是：

> **先做一个每一步都能被准确冻结、恢复、解释的 agent loop。**

这句话强制 v0 提前回答四个问题：

1. 进程在任意一行代码处 crash，重启后系统如何知道"运行到哪了"？
2. 用户 approve 之后，runtime 如何执行**当时被冻结的那个动作**，而不是重新推导？
3. 外部写操作执行到一半 crash，如何避免自动重复副作用？
4. 半年后回看一个 run，如何解释"模型当时为什么这么做"？

分层边界维持原方案不变：`core / llmd / toold / runtime / cli`，v0 全部 in-process，
包边界先拆好，进程边界以后再加。LLM 只提出意图，runtime 管状态和流程，toold 管工具
发现与执行。

## 1. 核心决策：事件为唯一权威（Ledger 模型）

v0 的持久化采用 **"事件为唯一权威 + 同步派生投影 + 校验聚合 + Anchor 检查点"**，
即把 event sourcing 做对，而不是绕开它。参考 tape.systems 的模型：
Tape = run 的事件流，Entry = RuntimeEvent，Anchor = checkpoint 事件，
View = ContextBuilder 的产出。

选择这个方向的判据：对 knuth 而言事件日志是**产品能力**（timeline、回放、解释、
将来 eval / self-awareness / workflow），不只是管道。

### 1.1 四条结构规则

**规则一：单一写入口。** 所有持久状态变更都通过 `RunLedger.apply(event)` 完成。
它在**一个 SQLite 事务**内：

1. 校验聚合不变量（见 §3）；
2. insert 事件行（seq 单调，`unique(run_id, seq)` 由 DB 保证）；
3. 同步更新派生投影表（runs / tool_invocations / approvals）。

`run.status` 不再有直写路径（删除 `RunStore.set_status` 公开接口）；状态是事件的
fold（见 §2.2）。**原子性是构造出来的，不靠纪律。** 同行评审第一条（事件与状态
双写不一致）在此结构下不再可能发生。

**规则二：事件是为重建而设计的决策事实，不是顺手记下的流水账。**
每一种"运行位置"都必须有显式事件表达（batch_planned / batch_closed /
invocation_started…）。禁止任何"扫描日志 + 启发式推断状态"的代码——如果某个状态
问题需要启发式才能回答，说明缺一种事件类型，应该补类型而不是补扫描。

**规则三：投影是派生缓存，不是权威状态。**
runs / tool_invocations / approvals 表可以随时 drop 掉从事件重建（`knuth admin
refold`）。改投影表结构不算数据迁移，refold 一遍即可。事件类型的形状才是终身契约。

**规则四：append 之前完成 redaction。**
日志 append-only 意味着 secret 一旦写入就**永远在那里**，"修正以新条目表达"不能把
已写入的明文变没。所以脱敏必须发生在 `apply()` 之前，没有事后补救（见 §8）。

### 1.2 ArtifactStore 是 Ledger 的 blob 侧店

事件 payload 只放最小事实；超过阈值（v0 定 8KB）的内容（工具结果、文件内容、
原始响应）写入 ArtifactStore，事件持有 `{artifact_ref, sha256, preview, size}`。
被事件引用的 artifact 是**不可变**的，run 存续期间不得回收——它们在语义上是
ledger 的一部分。会话消息（user / assistant 文本）属于"事实本身"，默认内联在
事件里（受 max_output_tokens 自然约束）；工具观测超阈值则外置。

### 1.3 Schema 演进纪律

- 事件类型一经发布（首个 tagged release 起），**形状冻结**。演进 = 追加新类型
  （或 `.v2` 后缀类型），不改旧类型。
- 所有 fold 必须容忍未知事件类型（跳过）和未知字段（忽略，`extra="allow"` 的
  正确用途就在这里）。
- 发布前的 v0 开发期允许 breaking change，但必须显式重置 DB（沿用现有的
  legacy-schema 守卫，发现旧形状直接报错而非静默兼容）。
- payload 最小化是对冲 schema 债的主要手段：`model.completed` 不再内嵌完整
  `InferenceMessage` 快照，而是存构成消息所需的最小事实（见 §2.1）。

## 2. 事件目录

### 2.1 Durable 决策事件（进 ledger）

| 类型 | payload（最小事实） | 说明 |
|---|---|---|
| `run.created` | `query, user_id?, metadata` | run 诞生 |
| `user.message` | `content` | 用户消息（含多轮续聊） |
| `run.resumed` | `cause: approval_resolved \| user_resume` | 状态回到 RUNNING 的显式事实 |
| `run.paused` | `reason, source: control \| hook` | 沿用 CONTEXT.md 的 RunPaused |
| `run.cancelled` | `reason, source` | 沿用 RunCancelled |
| `run.failed` | `error: ErrorInfo` | |
| `run.succeeded` | `answer, turns` | 聚合校验：无 open batch 时才允许 |
| `step.started` | `step_id, index, snapshot: ContextSnapshot` | 一次模型调用的开始，携带上下文快照（§6.3） |
| `model.completed` | `step_id, content, tool_calls: [{id, index, name, args}], finish_reason, usage` | 本轮模型产出的最小事实；不嵌整条 InferenceMessage |
| `model.failed` | `step_id, error` | |
| `model.aborted` | `step_id, reason` | |
| `tool.batch_planned` | `batch_id, step_id, calls: [{tool_call_id, index, name, args, args_hash}]` | "待办工作"成为显式事实 |
| `tool.proposed` | `tool_call_id, decision: allowed \| requires_approval \| denied, error?` | policy 决定 |
| `approval.requested` | `approval_id, tool_call_id, args_hash, title, reason, risk, preview`（preview 已脱敏） | |
| `approval.resolved` | `approval_id, resolution: approved \| denied, resolved_by?` | 不直接改 run 状态；resume 由 `run.resumed` 表达 |
| `tool.invocation_started` | `tool_call_id, idempotency_key, attempt` | |
| `tool.invocation_completed` | `tool_call_id, outcome: succeeded \| failed \| denied, observation \| observation_ref, meta` | observation 是模型将看到的文本（≤8KB 内联，否则 artifact ref） |
| `tool.invocation_marked_unknown` | `tool_call_id, reason` | 恢复程序对 in-flight 外部写的标记（§5.2） |
| `tool.batch_closed` | `batch_id` | 批次内所有 call 均有终态观测后追加 |
| `verification.failed` | `reason, feedback` | feedback 会被投影成会话消息（§7） |
| `context.compacted` | `replaces_through_seq, summary \| summary_ref` | **预留**：压缩也是追加的事实，v0 不实现 |
| `run.checkpoint` | `through_seq, state_ref` | **预留**：Anchor，fold 成本成为瓶颈时启用 |

### 2.2 状态 fold

`run.status` 是最后一个 status-bearing 事件的纯函数：

```text
run.created            -> CREATED
step.started           -> RUNNING
approval.requested     -> WAITING_APPROVAL
run.resumed            -> RUNNING
run.paused             -> PAUSED
run.cancelled          -> CANCELLED
run.failed             -> FAILED
run.succeeded          -> SUCCEEDED
```

注意 `approval.resolved` 不改状态：批准只是解锁，真正继续执行由 `RuntimeControl`
发起 resume 并追加 `run.resumed`。这保证"durable 状态"不依赖进程存活。

### 2.3 会话 fold（ConversationProjection）

发给模型的消息序列是事件的纯函数，规则封闭且 typed：

```text
user.message                -> user message
model.completed             -> assistant message（由 content + tool_calls 重组）
tool.invocation_completed   -> tool_result message（observation，必要时从 artifact 取回）
tool.proposed(denied)       -> （由对应 invocation_completed(outcome=denied) 覆盖，无独立消息）
verification.failed         -> user message（feedback 文本）   ← 反馈通路，见 §7
context.compacted           -> 替换 through_seq 之前的消息为 summary（预留）
```

**Provider 合法性不变量**（assistant 带 tool_calls 必须跟齐配对 tool_result）由
聚合在写入侧保证（§3），fold 因此无需防御性修补。v0 每次 build 全量 fold，
持久化缓存与 `run.checkpoint` 一起预留。

### 2.4 Transient 事件（不进 ledger）

InferenceEvent（content/reasoning/tool-call delta、流边界）与其对应的 transient
RuntimeEvent 投影，仅经 LiveRuntimeObservation 送达 listener（CLI 渲染、日志、
调试），不持久化。reasoning 维持现有 typed 事件（`inference.reasoning.*`），但
**原文默认不落盘**，仅 debug 模式下写入独立的 debug sink（非 ledger，见 §8）。

## 3. 聚合不变量（apply 时校验）

`RunLedger.apply()` 在事务内校验，违反即拒绝 append（编程错误，fail loud）：

1. `step.started` 仅当：无 open batch（batch_planned 而未 batch_closed），且
   run 状态 ∈ {CREATED, RUNNING}（经 run.resumed 解锁后）。
2. `tool.batch_planned` 仅当：对应 step 的 `model.completed` 已存在且
   tool_calls 非空，且无其他 open batch。
3. `tool.invocation_started` 仅当：该 call 的 `tool.proposed(allowed)` 存在，
   或 `approval.resolved(approved)` 存在**且 args_hash 与 approval.requested
   记录的一致**。批准"删 X"永远不会授权"删 Y"。
4. `tool.batch_closed` 仅当：批次内每个 call 都有终态观测
   （invocation_completed 任一 outcome；unknown 的 call 必须先被人工裁决为
   completed 后批次才能关闭）。
5. `run.succeeded` 仅当：无 open batch。
6. `approval.resolved` 仅当：对应 approval.requested 存在且未被 resolve 过。
7. seq 单调由 DB unique 约束兜底。

## 4. 派生投影与存储 schema

```sql
create table events (
  id text primary key,
  run_id text not null,
  seq integer not null,
  type text not null,
  step_id text,                  -- 分组列，timeline 有层级，为 workflow 留位
  event_json text not null,
  created_at text not null,
  unique(run_id, seq)
);

-- 以下全部为派生投影，可 drop 后 refold —— 表结构变更不是数据迁移

create table runs (
  id text primary key,
  status text not null,
  query text not null,
  last_seq integer not null,
  created_at text not null,
  updated_at text not null,
  data_json text not null
);

create table tool_invocations (
  tool_call_id text primary key,
  run_id text not null,
  batch_id text not null,
  step_id text not null,
  tool_name text not null,
  args_hash text not null,       -- sha256(canonical_json(args))
  status text not null,          -- proposed | awaiting_approval | approved | denied
                                 -- | running | succeeded | failed | unknown
  effect text not null,          -- pure | read | local_write | external_write | dangerous
  approval_id text,
  idempotency_key text,
  external_ref text,
  updated_seq integer not null
);

create table approvals (
  id text primary key,
  run_id text not null,
  tool_call_id text not null,
  args_hash text not null,
  status text not null,          -- pending | approved | denied | expired
  data_json text not null,
  created_at text not null,
  resolved_at text
);

create table artifacts (
  id text primary key,
  run_id text not null,
  kind text not null,
  sha256 text not null,
  uri text not null,
  metadata_json text not null,
  created_at text not null
);
```

ToolInvocation 投影合并了评审中的 `PendingAction` 与 `ToolExecutionRecord`
（两者字段重叠约九成，分开存会制造第三处需要一致的状态）。它的状态机就是
§2.1 中 tool.* 事件链的 fold。

## 5. Agent loop 与恢复语义

### 5.1 Loop 形状

Loop 是对投影的调度器，不含任何日志扫描启发式：

```python
while True:
    state = ledger.run_state(run_id)          # 读投影，一条 SQL

    if state.status in TERMINAL_OR_WAITING:    # paused/waiting_approval/终态
        return state.status

    if state.open_batch:
        # 恢复点：approve 后 resume、或 crash 后重启，都从这里继续
        for inv in state.open_batch.pending:   # status ∈ {proposed-allowed, approved}
            apply(tool.invocation_started)
            result = await broker.execute(inv)             # 带超时与取消，§9
            apply(tool.invocation_completed)
        if state.open_batch.awaiting_approval:
            return WAITING_APPROVAL
        apply(tool.batch_closed)
        continue

    view = await context_builder.build(ctx)    # fold + pipeline，§6
    apply(step.started, snapshot=view.snapshot)
    message = await stream_model(view)         # transient 事件走 observation
    apply(model.completed)

    if message.tool_calls:
        apply(tool.batch_planned)
        for call in message.tool_calls:
            decision = await policy.evaluate(call)
            apply(tool.proposed, decision)
            if decision is requires_approval:
                apply(approval.requested)      # 状态随 fold 变为 WAITING_APPROVAL
        continue

    verdict = verify(message)
    if verdict.ok:
        apply(run.succeeded); return SUCCEEDED
    apply(verification.failed, feedback=...)   # 投影成会话消息，模型下轮可见
```

关键性质：

- **approve 之后不重新 propose、不重新问模型。** `RuntimeControl.approve()` 追加
  `approval.resolved(approved)`（投影把 invocation 翻成 approved），resume 追加
  `run.resumed`，loop 重入后从 `state.open_batch` 直接执行被冻结的那条调用。
  原 `_pending_assistant_tool_message` 启发式扫描与 `reconstruct_messages_from_events`
  的防御性修补全部删除。
- deny 的处理：`approval.resolved(denied)` → 投影翻 denied → loop 为其 append
  `invocation_completed(outcome=denied, observation="Tool call denied: ...")`，
  模型下一轮看到拒绝并自行调整。

### 5.2 崩溃恢复（按 ToolEffect 分流）

启动或 resume 时，对 status=RUNNING 且无存活 invocation 的 run 执行恢复程序，
对每个 `running` 状态的 ToolInvocation：

```text
PURE / READ / LOCAL_WRITE:
  append tool.invocation_completed(outcome=failed, reason=crashed)
  —— 观测告知模型执行被中断，由模型决定是否重试

EXTERNAL_WRITE / DANGEROUS:
  append tool.invocation_marked_unknown
  —— 外部 API 可能已经成功（issue 可能已创建），自动重试 = 重复副作用
  —— UNKNOWN 必须人工裁决：
     knuth resolve <tool_call_id> --outcome succeeded|failed [--note ...]
     裁决追加 invocation_completed，批次方可关闭
```

`idempotency_key` 在 `invocation_started` 时生成并传入工具上下文；支持幂等的
外部 API 应使用它，使"重复执行"在源头无害。这不是完整的幂等与回滚体系
（rollback / compensation 仍按原方案预留），但封住了最危险的重复副作用。

## 6. Context pipeline

### 6.1 阶段固定顺序

```text
1. assemble     ConversationProjection fold + SystemSectionProvider 组装 preamble
2. redact       移除 secret / token / 私有 metadata —— 必须先于一切变换
3. compact      历史压缩、artifact offload（v0 仅保留阶段位，不实现复杂压缩）
4. tool_filter  按 policy 过滤可见工具
5. freeze       生成 ContextSnapshot，此后视图不可再修改
```

顺序固定解决"summarizer 在 redaction 之前看到 secret"这类次序事故。

### 6.2 扩展面（沿用 CONTEXT.md 已定的收权决定）

- `SystemSectionProvider`：第三方扩展上下文的**唯一**常规入口，只增不改。
- 全功率 `MessageMiddleware`：核心系统自用（redaction、compaction、tool filter
  的内部实现），不作为 plugin API 暴露。
- middleware 输出结构化 patch（谁删了什么、谁压缩了什么）作为 v0.5 审计增强预留。

### 6.3 ContextSnapshot（hash 级）

`step.started` 携带：

```python
class ContextSnapshot(KnuthModel):
    messages_hash: str        # sha256(canonical_json(messages))
    tools_hash: str
    preamble_hash: str
    model_config_hash: str
    message_count: int
    tool_count: int
```

成本接近零，回答"为什么模型当时这么做"时可证明两次 build 的输入是否一致。
完整版本跟踪（middleware_versions 等）等中间件真正多起来再加。

## 7. 验证与反馈通路

v0 的 Verifier 维持"非空即过"，但**失败必须有反馈**：`verification.failed`
事件携带 `feedback` 文本，ConversationProjection 把它投影成一条 user 消息
（如 "Your previous answer was empty. Provide a concrete answer or call a tool."），
模型下一轮据此调整。没有反馈通路的重试一律禁止——那是烧 token 的空转。
`knuth.finish` 控制工具方案评估过，v0 不采用（让普通对话变重），保留为未来选项。

## 8. 安全红线（v0 地基，不是后期功能）

1. **redact-before-append**：脱敏在 `RunLedger.apply()` 之前完成。append-only
   日志中的明文 secret 无法事后清除。
2. secret 明文不进 ledger、不进 ArtifactStore；工具通过 secret handle 取用，
   模型与事件只见 handle 描述。
3. `approval.requested.preview` 必须经过脱敏。
4. raw provider response 默认不持久化；debug 模式写入独立 debug sink
   （`~/.knuth/debug/`，非 ledger，可整目录删除）。reasoning 原文同此。
5. `EntryPointToolProvider` 默认关闭，`--enable-plugins` 显式开启；entry point
   等于在主进程执行第三方代码，只适合用户显式信任的插件。Subprocess / MCP
   provider 是非信任工具的正道（沿用原方案的 provider 分级，时间表不变）。

## 9. 工具模型

### 9.1 数据与执行分离

`ToolBase(BaseModel)` 废弃。Pydantic 只描述数据，执行器是普通 class：

```python
class ToolManifest(KnuthModel):          # 数据：给 registry / policy / LLM spec
    name: str
    description: str
    parameters: dict
    parallelable: bool = False
    cacheable: bool = False
    effect: ToolEffect = ToolEffect.READ
    risk: ToolRisk = ToolRisk.LOW
    timeout_s: float | None = None

class Tool(Protocol):                    # 执行：普通对象，可持有 client/sandbox 句柄
    @property
    def manifest(self) -> ToolManifest: ...
    async def invoke(self, invocation: ToolInvocation, ctx: ToolRuntimeContext) -> ToolResult: ...
```

`ToolRuntimeContext` 分两半：数据（run_id、tool_call_id、workspace_uri、
idempotency_key，Pydantic）+ 能力句柄（put_artifact、emit_progress、
cancellation checkpoint，普通对象）。流式输出的 shell、要写 artifact 的工具
由此有了落点。

### 9.2 执行边界

- `broker.execute` 包 `anyio.move_on_after(manifest.timeout_s)`，cancel scope
  与 RunInvocation 共享——abort 信号从此覆盖工具执行，不再只覆盖模型流。
  超时 → `invocation_completed(outcome=failed, reason=timeout)`。
- 并行维持原方案保守策略：v0 串行执行，保留 `parallelable`，将来"批内全部
  parallelable 才并行、按 index 顺序回填观测"。
- 路径沙箱统一用 `Path.is_relative_to`（现实现已正确），禁止 `str.startswith`。

## 10. Hook 与观测（确认 CONTEXT.md 现状，不再扩权）

- `RuntimeEventListener`：第三方可注册，只读，永不影响主流程。
- `BlockingHook`：仅核心系统注册，仅在 run-state transitions 与外部副作用前夕，
  仅返回 continue / pause / terminate。**没有 MUTATE**——数据变更走各自的显式
  seam（SystemSectionProvider、PolicyEngine、ToolBroker）。
- 原方案第 12 节的 14 个 hook point 与 `HookAction.MUTATE` 正式作废。

## 11. 与旧方案的差异清单

| 旧方案 | 本设计 |
|---|---|
| EventStore + RunStore 各自直写 | `RunLedger.apply()` 单一写入口，单事务，状态即 fold |
| 从事件启发式重建消息与 pending 状态 | 决策事件 + typed fold + 聚合不变量，禁止启发式 |
| approval 后恢复路径未定义 | batch/invocation 事件链冻结动作，approve 后执行原调用 |
| `model.completed` 内嵌完整消息快照 | 最小事实 + 超阈值 artifact 外置 |
| crash 恢复未定义 | effect 分流：可重试 / UNKNOWN 人工裁决 + idempotency_key |
| approval 以 run_id+tool 名标识 | 绑定 tool_call_id + args_hash，执行前校验 |
| 验证失败无反馈直接重试 | verification.failed 投影为反馈消息 |
| hook 可 pause/terminate/mutate，14 个点 | 观测与控制分离，无 MUTATE（CONTEXT.md 已定） |
| middleware 任意改写 view | 阶段固定 pipeline + SystemSectionProvider 唯一第三方入口 |
| 安全红线未提 | redact-before-append 等五条为 v0 地基 |
| entry points 默认开 | 默认关，`--enable-plugins` |
| ToolBase(BaseModel) 数据执行混合 | ToolManifest（数据）/ Tool Protocol（执行）分离 |
| RuntimeEvent 三字段 namespace/name/type | 单一 `type`（实现已是） |

## 12. 实施顺序

按"不修会出数据事故"排序：

1. **事件目录 + 聚合 + `RunLedger.apply()`**：单事务内 events insert + 投影
   更新；状态改为 fold，删除 set_status 直写。
2. **batch / invocation 事件链 + 投影表**：重写 loop 为投影调度器；删除
   `_pending_assistant_tool_message` 与旧 `reconstruct_messages_from_events`。
3. **approval 闭环**：args_hash 绑定、resolved→resumed 链路、deny 观测回填。
4. **崩溃恢复**：effect 分流、UNKNOWN、`knuth resolve`、idempotency_key。
5. **verification feedback**。
6. **安全红线**：redact-before-append、preview 脱敏、debug sink、entry points
   默认关。
7. **工具模型拆分 + 超时/取消**。
8. **context pipeline 阶段化 + ContextSnapshot**。

预留不实现（事件类型留位即可）：`run.checkpoint`（fold 成本成为瓶颈时）、
`context.compacted`、daemon / worker lease、Workflow IR、tool cache、
rollback / compensation、`knuth.finish`。

## 13. 待办：语言同步

实施第 1–2 项时，向 CONTEXT.md 增补术语：**RunLedger**（单一写入口与权威事件
流）、**DecisionEvent**（为重建而设计的 durable 事实）、**Projection**（派生
可重建缓存）、**ToolInvocation**（工具调用状态机投影）、**ToolBatch**（一轮
模型产出的待办工作集）、**ContextSnapshot**（冻结的上下文证明）、
**UnknownOutcome**（外部写中断后的人工裁决态）。
