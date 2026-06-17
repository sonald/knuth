# MessageTape 身份与投影重构 —— 决策确认文档

> 状态：**已确认**（2026-06-17）。本文汇总一次设计讨论的全部结论。所有 `[确认]` 决策
> 已拍板；唯一本期不做的是 **归属命名空间/防伪**（§5.1.1、§7.2），留到引入真正第三方
> 加载器时再封。代码引用基于当前 `main`（`packages/knuth-runtime`、`packages/knuth-core`）。

## 背景

`message middleware rewrites` 落地后，围绕 `TapeMessage` / `MessageTape` 这套中间
表示做了一轮审视，核心问题是：

- 这两个类型有多少是真实领域概念，多少是冗余/过度设计？
- tape 上每条消息的 **身份（id）** 由谁拥有？当前由各 middleware 自己铸造，
  能否保证唯一、能否扛第三方 middleware？
- read API 有两条几乎平行的 fold，能否收敛到一条？

下面按主题给出结论。主线是一句话：

> **tape 上每个 item 的身份（id / rewrite_id / origin / seq）全部是 append
> 顺序的 runtime 派生量，第三方在类型上无法提供；第三方只描述“做什么操作、改哪条、放什么
> 内容”。**
>
> **本期例外 —— 归属名（`middleware`）不在上述保证内**：它由 runner *代填* 而非第三方在
> patch 里直接给，但取的仍是作者自报的 `cls.name`，**归属伪造本期尚未封死**（注册表命名空间
> 延后，§5.1.1 / §7.2）。不要把它和 id/origin 那几个“不可伪造”项混为一谈。

---

## 1. 身份归 runtime 强制注入（核心决策）

### 1.1 现状与问题

- `TapeMessage.id` 是整个改写机制的寻址主键：`suppresses` / `ReplacePatch.target_ids`
  / `InsertPatch.position.target_id` 都按 id 引用消息。
- **唯一性是硬需求**：撞 id 会让 `_apply_rewrite_records`
  （[context.py:461](../packages/knuth-runtime/src/knuth_runtime/context.py:461)）同时
  suppress 两条；`order.index(target_id)` 只命中第一条 → 语义歧义。
- 现状唯一性靠 **作者约定 + fail-stop 兜底**：每个 middleware 自己拼
  `rewrite_id`（[middleware.py:347](../packages/knuth-runtime/src/knuth_runtime/middleware.py:347)、
  [434](../packages/knuth-runtime/src/knuth_runtime/middleware.py:434)、
  [512](../packages/knuth-runtime/src/knuth_runtime/middleware.py:512)），靠往 id 里塞
  中间件名 + 目标 id + 内容摘要去避撞；唯一护栏是 ledger 的
  `duplicate rewrite_id` / `duplicate message id`
  （[ledger.py:966](../packages/knuth-runtime/src/knuth_runtime/ledger.py:966)、
  [1045](../packages/knuth-runtime/src/knuth_runtime/ledger.py:1045)）——一旦撞了就是
  运行中 `LedgerError` 崩 run。对第三方插件作者是 pit of failure。

### 1.2 决策

`[确认]` **让 minting 不可表达，而不是被拒绝。** 护栏从“运行时 `_require` 报错”
上移到“类型上根本没有让第三方写 id 的字段”。落在两个输入面：

1. **patch 类型 payload 化**：`InsertPatch.items` / `ReplacePatch.replacement_items`
   不再是 `list[TapeMessage]`（带强制 `id`），改为 **payload-only**——第三方只交
   `InferenceMessage` + 语义 metadata，**没有 id / rewrite_id / origin / visibility
   字段可填**。
2. **draft event 去身份字段**：`MessageRewriteAnchorDraft.rewrite_id`、
   `MessageRewriteMessageDraft.message_id` 从 **输入 draft** 删除。begin/messages/end
   已强制连续原子块（[ledger.py:365-369](../packages/knuth-runtime/src/knuth_runtime/ledger.py:365)），
   reducer 按 **位置** 即可识别三段，无需作者给 correlation key。
3. **堵 metadata 后门（评审 P2）**：去掉字段还不够——payload 允许的“语义 metadata”是个
   自由 dict，第三方仍能塞 `rewrite_id` / `message_id` / `origin` / `visibility` /
   `middleware` / `suppresses` / `operation` / `position` / `kind` 污染审计。规则：这组
   **runtime-reserved keys 在 patch metadata 里出现即拒绝**；第三方自由 metadata
   只能放进子对象 `metadata["semantic"]`（runtime 身份单独写 `metadata["_runtime"]` 或
   顶层保留键，且 **runtime 写在后、必覆盖**）。审计视图只信 runtime namespace 的身份。

### 1.3 id 分配方案（覆盖所有路径）

`[确认]` `rw:{seq}` 只盖 durable，必须把所有路径补齐：

| 路径 | id 来源 | 唯一性保证 |
|---|---|---|
| ledger 消息 | `m:{seq}`（已是） | seq 单调，构造性无撞 |
| durable rewrite | **ledger append/store 层、block-aware 派生** `rw:{begin.seq}` / `rw:{begin.seq}#{ordinal}` | 同上，且 replay-stable（seq 已落库） |
| ephemeral rewrite（AgentsMD 等） | runner 派生 `eph:{checkpoint}:{patch_ordinal}#{message_ordinal}`，`patch_ordinal` 是 **runner 在本次 build 内自增的全局序号**（不依赖 `middleware.name`） | **仅保证单次 `ContextBuilder.build()` 内唯一，不承诺跨 build 稳定** |

> **durable 的派生点是 store 层，不是普通 reducer（评审 P2）**：`_apply_many_in_txn`
> 当前是逐 draft 的 map（[ledger.py:1187](../packages/knuth-runtime/src/knuth_runtime/ledger.py:1187)），
> 每个 draft 只拿到自己的 seq；而 `rw:{begin.seq}` 要求 begin 之后的 message/end 引用
> **begin 的 seq**。reduce 阶段虽已能拿到自己的 `seq`，但拿不到“别人的 seq”，所以单条
> reduce 里独立完成不了。这层要改成 **block-aware**：识别连续 rewrite 块、捕获 begin 的
> 已分配 seq、给整块 stamp。
>
> **ephemeral 不进 ledger、拿不到 seq（评审 P1/P3）**：必须单列方案；不稳定不是错（它本就
> 不 durable），但要把目标写死为“build 内唯一”。**唯一性绝不能依赖 `middleware.name`**——
> 它是自报的（§5.1.1 归属防伪本期延后），两个同名 middleware 在同一 checkpoint 各产第 0 个
> patch 就会撞。所以唯一部分用 **runner 自持的 `patch_ordinal`**（本次 build 内单调自增，
> 跨 middleware 全局唯一），`message_ordinal` 是 patch 内位置；`middleware.name` 只放
> `metadata["semantic"]` 里方便人看，不参与 id。

### 1.4 派生关系收尾

- `[确认]` `message_id` 作为传输字段消失——由 **`rewrite_id` + 连续块内的 message ordinal
  （位置）** 派生，即 `rw:{begin.seq}#{message_ordinal}`。**不需要 event 上的 `index` 字段**。
- `[确认]` `index` 字段 **直接删**（评审 P3）：当前就是死字段
  （[middleware.py:223/249](../packages/knuth-runtime/src/knuth_runtime/middleware.py:223)
  写入，reconstruct 与 ledger fold 都不读，顺序靠 event append 序）。store 层按块内位置
  数 ordinal，不再从字段读 —— 删 index 与“message_id 由 ordinal 派生”是同一件事，不矛盾。
- `[确认]` `m:{seq}` 约定当前 **双份硬编码**
  （[context.py:275](../packages/knuth-runtime/src/knuth_runtime/context.py:275) 与
  [ledger.py:226](../packages/knuth-runtime/src/knuth_runtime/ledger.py:226)）→ 抽成
  一个共享函数，避免“写时校验的投影”与“实际投影”静默分叉。

### 1.5 schema 重构后果（**不考虑兼容**）

`[确认]` 尚未发布，**不保留旧 event 兼容**：直接重构 schema，dev 库可清，不写兼容
shim、不为旧 JSON 加 parse 回归测试。

- stored event **仍需** rewrite_id（reconstruct 关联、audit 显示），只是从“draft 提供”
  变“**store 层赋值**”（见 §1.3）。当前 stored 由 draft 继承
  （`class MessageRewriteAnchor(MessageRewriteAnchorDraft, StoredRuntimeEventBase)`，
  [runtime_events.py:450](../packages/knuth-core/src/knuth/core/runtime_events.py:450)）共享字段；
  draft 删字段后拆继承：身份字段下沉到 stored 类（或中间层），draft 只剩输入字段。
- **本地 ledger 库口径（评审 P2）必须一起改，不能只改 Pydantic**：现有
  `_guard_schema`（[ledger.py:1474](../packages/knuth-runtime/src/knuth_runtime/ledger.py:1474)）是
  **列级**检查，事件存在 `data_json` blob 里——我们只改 JSON 形状、不动列，**guard 拦不住**；
  叠加 `extra="ignore"`，旧 `message.rewrite_*` 事件会 **parse 成功但被错读**（丢
  `message_id`、id 被重新派生），比干净失败更糟。**决定：bump guard 让旧库响亮失败**——
  加一个 `schema_version`（pragma 或哨兵列）并在 `_guard_schema` 校验，复用现有
  “remove the legacy database or use a new one”路径；dev 直接清掉本地 ledger 库。
- 测试只覆盖 **新 schema** 的 store → parse → reconstruct round-trip，不背旧数据包袱。

---

## 2. TapeMessage 瘦身

`TapeMessage` 是 load-bearing 的（承载 id + 出处，是 `InferenceMessage`
故意不具备的“可编辑投影行”），**保留**，但削成只剩不可约的 overlay：

| 字段 | 处理 | 依据 |
|---|---|---|
| `source_event_seq` | `[确认]` 删 | 全仓 **从没被读**，且就是 `event.seq` 的副本 |
| `middleware_name` | `[确认]` 删 | 全仓 **从没被读**，provenance 走 `metadata` + 归属 |
| payload（role/content/tool_*） | `[确认]` 改为组合 `message: InferenceMessage` | 现在 reconstruct 把 `InferenceMessage` 拆成 5 个平铺字段（[context.py:364](../packages/knuth-runtime/src/knuth_runtime/context.py:364)），`to_inference_message()` 又逐字段拼回（[context.py:64](../packages/knuth-runtime/src/knuth_runtime/context.py:64)）——纯仪式，且 **有损**：`InferenceMessage.name`（[messages.py:62](../packages/knuth-core/src/knuth/core/messages.py:62)）在往返中被静默丢弃 |
| `id` / `origin` / `metadata` | 保留 | 不可约的投影 overlay；其中 id/origin 全部 runtime 注入（见 §1、§4） |

瘦身后 `to_inference_message()` 退化为 `return self.message`。

---

## 3. MessageTape 不再是空壳

`MessageTape` 当前是单字段包装 `{ items: list[TapeMessage] }`，无 validator / 方法，
所有行为散在自由函数里，且 `_model_visible_items` 在两个文件各有一份重复
（[middleware.py:308](../packages/knuth-runtime/src/knuth_runtime/middleware.py:308) 与
[context.py:384](../packages/knuth-runtime/src/knuth_runtime/context.py:384)）。

`[确认]` 让它名副其实：把投影行为收成方法/集中函数，并消掉重复——

```python
class MessageTape(KnuthModel):
    items: list[TapeItem]

    def raw_ledger_messages(self) -> list[InferenceMessage]: ... # origin == LEDGER
    def model_context(self) -> list[InferenceMessage]: ...      # TapeMessage 且未被 TapeAnchor suppress
    def model_visible(self) -> list[TapeMessage]: ...           # 现 _model_visible_items，单一来源
    def with_records(self, records) -> MessageTape: ...
```

（代码库已有“模型带方法”的先例：`TapeMessage.to_inference_message`、
`InferenceMessage.to_litellm_message`，不违背风格。）

---

## 4. 身份/投影词汇 typed 化

`[确认]` 把裸字符串契约收成 `StrEnum`，且这些枚举 **只由 runtime 填充**，第三方无法
引入新值：

```python
class TapeItemSource(StrEnum):
    LEDGER = "ledger"
    MIDDLEWARE = "middleware"
```

理由：

- **纯内存、零数据迁移**：`origin` 从不进 metadata、从不落库，只是 `TapeMessage` 的构造期字段。
- **当前不一致**：event 层同类判别式早已是 `Literal`
  （[runtime_events.py:222/224](../packages/knuth-core/src/knuth/core/runtime_events.py:222)），
  tape 层退化成裸 `str` 是同一子系统里的类型倒退。
- 选 `StrEnum` 而非裸 `Literal`：需要在多个文件按 **符号** 引用
  （`origin == TapeItemSource.LEDGER`）；`StrEnum` 仍是 `str` 子类，序列化/比较行为不变，
  纯加固。`StrEnum` 也是这仓家法（`ToolRisk` / `ApprovalStatus` /
  `MessageMiddlewareCheckpoint` 等）。

`[确认]` **`internal_anchor` role 哨兵删除**：grep 显示它
**只有写入、无任何条件读** → 伪装成 load-bearing 的死哨兵。正确做法不是清空 role 字符串，
而是让 anchor 不再冒充 `TapeMessage`：

```python
TapeItem = TapeMessage | TapeAnchor   # MessageTape.items: list[TapeItem]
```

`TapeAnchor` 只承载投影真正读取的 suppress marker（`id` + `suppresses`），`TapeMessage`
回归纯消息。anchor 不再是 message → projection 按 **类型** 自然跳过，`internal_anchor`
随之消失。rewrite_id / kind / middleware / operation / position / metadata 仍保留在 event
和 audit 视图里，不复制到 tape anchor。

---

## 5. 第三方 middleware 的能力边界

`[确认]` 收敛后的 API 面与所有权切分：

| 第三方能给 | runtime 独占注入 |
|---|---|
| operation（insert / replace） | id（`rw:{seq}#i` / `eph:...`） |
| targets：`suppresses` / `position.target_id`（**入站引用** runtime-owned 的现有 id，允许） | rewrite_id |
| payload：`InferenceMessage` | origin = `TapeItemSource.MIDDLEWARE` |
| 语义 metadata（只进 `metadata["semantic"]`，§1.2.3） | `TapeAnchor` suppress marker |
| | 归属中间件名（本期自报 `cls.name`，§5.1.1 延后） |
| | source seq |

**入站引用为何安全**：durable id 是 `*:{seq}`、seq append-only，被引用的 id 语义永不
漂移；唯一会失效的是“已被 suppress”，而 ledger 已在校验（target 存在 + 未被 suppress +
连续 span，[ledger.py:974-996](../packages/knuth-runtime/src/knuth_runtime/ledger.py:974)）。
**只有出站铸造改变，入站引用照旧稳。**

### 5.1 “id 强制注入”不覆盖的两个边界（须一并处理）

1. `[确认 → 本期延后]` **归属伪造**：anchor 的 `middleware` 字段当前由 runner 用
   `middleware.name` 填（已是 runner 自取，方向对），但 `name` 仍是作者自填的类属性，
   第三方可声明 `name = "context_compaction"` 冒充。要扛第三方，归属名须由
   **注册表 / 插件命名空间** 分配（带 plugin 前缀），不信自报 `cls.name`。
   **决定（§7.2）：本期只做 id 强制注入，归属防伪留到引入真正第三方加载器时再封**——
   现在没有第三方加载器，提前做注册表是为尚不存在的信任边界投资。本期接受 `middleware`
   仍是自报 `cls.name`。
2. `[确认]` **payload 合法性**：role 仍在第三方 payload 里（如试图注入
   `role="system"`），id 注入管不到；唯一闸是 `validate_provider_messages`
   （mid-conversation 的 system 非 leading → 被拒）。**明确界定：强制注入锁的是“身份与
   归属”，不是“内容合法性”，后者仍骑在 provider-valid 校验上。**

---

## 6. Read 视图：一条 fold，两个 projection policy

`[确认]` 收敛重复 fold，但 **保留语义区分**（不可把 `messages()` 偷偷切成 model
projection，[design doc:565](message-middleware-requirements-and-design.md:565)）。

- `reconstruct_message_tape_from_events` = **唯一 fold**。
- 从同一 tape 派生两个 policy，**判别式用 origin / item 类型，不用 `apply_suppression`
  bool**：

```python
def raw_ledger_messages_from_tape(tape):         # = AgentRuntime.messages()
    return [i.to_inference_message() for i in tape.items
            if isinstance(i, TapeMessage) and i.origin == TapeItemSource.LEDGER]

def model_context_messages_from_tape(tape):      # = AgentRuntime.model_context_messages()
    suppressed = {tid for i in tape.items if isinstance(i, TapeAnchor)
                       for tid in i.suppresses}
    return [i.to_inference_message() for i in tape.items
            if isinstance(i, TapeMessage) and i.id not in suppressed]
```

`[确认]` **命名不要骗人（评审 P5）**：raw helper 叫 `raw_ledger_messages()`，**不叫**
`raw_conversation()`。前者是“ledger message-like projection”，**包含 `verification.failed`**
（这也是 **保持现状**——旧 `reconstruct_messages_from_events` 就把它折成 user 消息，
[context.py:242](../packages/knuth-runtime/src/knuth_runtime/context.py:242)）。未来若 UI / IM
要“干净的用户可见历史”，另起一个 **显式命名** 的 `user_visible_history()` 按需过滤，不要污染
raw 的语义。design doc:560 那句 “user/assistant/tool result/notice” 是举例非穷举，按本决定
对齐即可。

> **为何不能用 `apply_suppression=False`**：fold 已把 durable rewrite 合进
> `tape.items`，注入替身也是 model-facing `TapeMessage`。只关 suppression 会同时返回
> 原始项 + 注入替身 = **同段内容双计**，不符合 raw 定义。raw / model 是两个独立轴
> （含不含注入、丢不丢被 suppress 的原始项），单个 bool 会把它们混成一个。
> 正确判别式：raw = `origin == LEDGER`（自动排除注入项与 anchor，且不碰 suppression →
> 被 suppress 的原始项照样保留）。

`[确认]` **删平行 fold**：`reconstruct_messages_from_events`
（产 `list[InferenceMessage]`）已删除；`AgentRuntime.messages()` 改走 tape 的
`raw_ledger_messages()` 投影。该投影与旧 fold **逐事件等价**（同五种事件、同字段映射，且
`_apply_rewrite_records` 只插入不重排 → ledger 项相对顺序不变）。测试迁到
`raw_ledger_messages_from_events`。

---

## 7. 已收敛问题

> 评审 P4 / P5 已收敛掉原 Q1、Q4；本期只留下归属命名空间延后。

1. **~~`internal_anchor` 哨兵~~ → 已定（评审 P4）**：已引入 `TapeItem` union，
   anchor 不再冒充 message；`TapeVisibility`/`visibility` 字段一并消除。
2. **~~归属名命名空间~~ → 已定**：**先只做 id 强制注入；归属伪造留到引入真正第三方
   加载器时再封**（§5.1.1）。注意 ephemeral id **不** 依赖归属名——唯一性走 runner 的
   `patch_ordinal`（§1.3 / 评审 P1），归属延后不影响其正确性，`middleware.name` 仅入
   `metadata["semantic"]`。
3. **~~raw 视图边界 / verification.failed~~ → 已定（评审 P5）**：raw helper 命名为
   `raw_ledger_messages()`，**含** `verification.failed`（保持现状）；用户可见过滤另起
   `user_visible_history()`。无需再拍。

---

## 8. Blast radius 与建议顺序

决策（§1）排除了“最小版（runner 读 metadata）”，承诺走“最深版（身份下沉到 store 层、
删传输字段）”。改动面：

| 文件 | 改动 |
|---|---|
| `knuth-core/.../runtime_events.py` | 拆 draft/stored 继承，draft 去 `rewrite_id`/`message_id`/`index`；stored 只保留并由 store 层填 `rewrite_id`/`message_id`，`index` 直接删除 |
| `knuth-runtime/.../ledger.py` | `_apply_many_in_txn` 改 **block-aware**：识别 rewrite 块 + 派生 `rw:{begin.seq}#{ordinal}`（block 内位置）；抽 `m:{seq}` 共享函数；**`_guard_schema` bump（加 `schema_version`/哨兵列）让旧库响亮失败**（§1.5） |
| `knuth-runtime/.../middleware.py` | patch 类型 payload 化 + **拒绝 runtime-reserved metadata 键**（§1.2.3）；runner 注入身份（归属暂用自报 `cls.name`，§5.1.1 延后）；ephemeral 派生 `eph:{checkpoint}:{patch_ordinal}#{message_ordinal}`（patch_ordinal = runner build 内全局自增） |
| `knuth-runtime/.../context.py` | `TapeMessage` 组合 `InferenceMessage` + 删纯内存死字段；`TapeItemSource` enum；`TapeItem`/`TapeAnchor` union；删 `visibility`；`MessageTape` 收方法；reconstruct 读派生 id；删平行 fold |
| `knuth-runtime/.../agent.py` | `messages()` → `raw_ledger_messages`，`model_context_messages()` → model policy |
| 测试 | 迁到 raw projection helper；新增 **撞 id 不可表达**（含 metadata 后门）、身份注入、`name` 丢失修复、ephemeral 同名撞 id 防回归、**新 schema round-trip**（§1.5，不背旧数据） |

建议顺序（每步可独立验证）：

1. **enum（`TapeItemSource`）+ `m:{seq}` 共享函数 + 删 **纯内存**
   死字段（`source_event_seq` / `middleware_name`）**——零数据迁移、低风险，先落。
   *不含* `internal_anchor` 与 `index`（见步 3/4）。
2. **TapeMessage 组合 `InferenceMessage`**——修掉 `name` 丢失，去掉拆-拼往返。
3. **身份下沉（event schema 重构，评审 P1/P2；不考虑旧数据兼容，§1.5）**：拆 draft/stored 继承、
   draft 去 `rewrite_id`/`message_id`/`index`、**store 层 block-aware 派生 `rw:{seq}`**、
   patch payload 化 + **metadata 保留键拒绝**、runner 注入身份、ephemeral `patch_ordinal` 派生、
   **bump `_guard_schema`**。**测新 schema round-trip 即可**。
4. **`TapeItem`/`TapeAnchor` union（评审 P4）**：anchor 不再冒充 message → 删 `internal_anchor`
   → **删 `visibility`**（目标态，§4），model projection 改按 item 类型 + suppress set。
5. **read 视图收敛**：唯一 fold + 两 policy（`raw_ledger_messages` / `model_context`），
   删平行 fold，迁测试。
6. **归属命名空间 —— 本期不做**（§5.1.1 / §7.2），留到引入第三方加载器时再封。
