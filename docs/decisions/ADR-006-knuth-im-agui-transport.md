# ADR-006: knuth-im 与 AG-UI transport 边界

## 状态
Accepted

## 日期
2026-06-14

## 背景

Knuth 的第一个参考前端是 knuth-cli（终端 REPL）。第二个参考前端是 **knuth-im**：一个基于 CopilotKit 的、带 UI 和生成式能力的 Web agent。它要复用同一套 runtime，而不是另起一套编排。

CopilotKit 通过 [AG-UI 协议](https://docs.ag-ui.com)（一条类型化的 SSE 事件流,事件按 lifecycle/text/tool/state/custom/reasoning 分类,base 属性只有 `type`/`timestamp`/`rawEvent`）连接 agent。AG-UI 本身没有 Knuth 的 durable/transient 语义,也没有事件日志的概念（`MESSAGES_SNAPSHOT` 是 state/message 同步,不是 durable event log）;durable/transient 是 Knuth 自己在 `RuntimeEvent` 上的区分,翻译时由 `knuth-agui` 决定哪些 Knuth 事件映射成哪类 AG-UI 事件。即便如此,两者结构高度对齐,`RunSession` 的 `RuntimeEventListener` + `RuntimeEventInterest` 观察层（ADR-004）就是天然的流式数据源。M1 spike（`knuth-agui` 包：FastAPI + SSE + 事件翻译）已用真实模型验证了这条链：`RUN_STARTED → THINKING_* → TEXT_MESSAGE_* → TOOL_CALL_* → RUN_FINISHED` 的流式翻译成立，anyio/FastAPI 并发模型成立。

剩下的问题不是"能不能"，而是"边界划在哪"：哪些东西进 `knuth-agui`、哪些进 runtime、哪些进前端；会话生命周期怎么建模；危险工具和前端工具怎么走人审与回传。本 ADR 记录这些决策，使其不再是 spike 里的隐式选择。

CONTEXT.md 已规定 runtime 的领域语言（`RuntimeEvent`、`RuntimeEventListener`、`ToolProvider`、`ToolInvocation`、`UnknownOutcome` 等）。本 ADR 在不改写这些概念的前提下，给 Web 形态划边界。

## 决策

### 1. 产品形态:单用户本地 dev 助手 Web 版

knuth-im v1 是单工作区、单用户、跑在本机或个人服务器的开发助手——CLI 的 GUI 替身。**不做** auth、多租户、会话隔离。复用 knuth-cli 的工具集（`create_cli_tool_provider`）与系统提示，沿用 SQLite ledger。多用户/多租户若将来需要，是独立演进，不进 v1。

### 2. 拓扑:浏览器直连 knuth-agui,无 Node 运行时

前端用 `@ag-ui/client` 的 `HttpAgent` 直接打 `knuth-agui` 的 AG-UI 端点，**不引入** CopilotKit 的 Node CopilotRuntime。

推论:没有 Node 中间层来补协议或纠漂移,因此 `knuth-agui` 必须自己把 AG-UI 实现完整,并尽早改用官方 `ag-ui` Python encoder（而非 spike 里手搓的事件构造）。前端声明的工具(`RunAgentInput.tools`)也必须由 `knuth-agui` 自己解析并登记到它提供的 AG-UI client `ToolProvider`。

### 3. 边界铁律:运行时只长加性、与受众无关的 seam

当 `knuth-agui` 需要 runtime 尚未暴露的能力时,**runtime 可以演进,但只能是加性的、与受众(CLI/Web/daemon)无关的 seam**;runtime 永不认识 HTTP、SSE、AG-UI、CopilotKit。`knuth-agui` 永远是纯 transport——它翻译事件、路由控制调用、做 ledger 只读查询,不持有 agent 策略,也不把 Web 语义泄漏进 runtime。

这条规则与 ADR-004（`RuntimeControl` 用显式操作而非重载入口）、ADR-005（访问控制属于 policy 而非工具）一脉相承:新的上下文来源是新的 provider,新的能力是新的中性参数,而不是给既有面塞特例。

**组合点(关键)**:`knuth-agui` 的入口接收一个已经构造好的 `AgentRuntime`;自己绝不构造 runtime、绝不决定 server-side prompt / tool provider / policy。"knuth-im 复用 knuth-cli 的工具集与系统提示"(决策 1)这件事发生在 **knuth-im 的 host / runtime factory** 层(与 knuth-cli 各自构造自己的 runtime 是对等关系),不在 transport 包里。AG-UI client tools 是例外但不是 agent 策略:其 `ToolProvider` 类型由 `knuth-agui` 定义,host 显式创建同一个 provider,注册进 runtime,再传给 `create_app(runtime, client_tool_provider=...)` 供 transport 登记请求中的 AG-UI tool schema。换言之:server 工具、prompt、policy 归 host;AG-UI client tool schema 的协议适配归 `knuth-agui`。

> 注:M1 spike 里 `knuth-agui/runtime_factory.py` 的 `build_spike_runtime` 违反了这一点(把 runtime 构造放进了 transport 包)——这是 spike 便利,M2/M3 必须把 runtime factory 迁出 `knuth-agui`,落到 knuth-im host(`apps/knuth-im-web` 的后端入口或独立 `knuth-im` host 模块),`knuth-agui` 只保留 transport app 与 AG-UI client tool provider。

本 ADR 据此预批以下 seam(实现时逐个加,带测试):

- **调用方指定 run id**:在公共控制面 `AgentRuntime.start(prompt, run_id=None)` 加可选 run id,经 `RunSession._prepare_run_id` 透传到 `ledger.create_run(query, run_id=None)`。当前公共路径 `start(prompt)` → `RunSession._prepare_run_id()` → `ledger.create_run(prompt)`([agent.py](../../packages/knuth-runtime/src/knuth_runtime/agent.py)、[session.py](../../packages/knuth-runtime/src/knuth_runtime/session.py))调用方没有传 id 的入口,所以只改 ledger 不够,必须把可选参一路加到 `start`。ADR-004 已预见此路径("如果未来需要先创建 run 再启动 invocation,可以另行引入 `create_run + drive` 高级 API");二选一:优先 `start(prompt, run_id=None)`,若将来需要"先建 run 再驱动"再上显式 `create_run + drive`。(支撑决策 4)
- **AG-UI client ToolProvider**:前端声明的工具不走 `start/resume(..., tool_providers=...)` 这类 invocation overlay。`knuth-agui` 提供一个普通 runtime-wide `AGUIClientToolProvider`,host 在构造 runtime 时显式注册;`/agent` 收到 `tools` 后只把 schema 登记到这个 provider。这样 `ToolBroker` 只有一条工具来源:runtime 的 `ToolRegistry`。同名 client tool 重复登记必须 schema 相同,否则 fail fast,避免一个 durable tool name 在不同请求里漂移。
- **客户端工具的暂停/补录态**(见决策 6;**新中性概念,不复用 `UnknownOutcome`**)。
- 历史重建已具备(`reconstruct_messages_from_events`),无需新 seam。

### 4. 会话(“S”):耦合模型,threadId == run_id,无 SessionManager

- **耦合**:一条 SSE 连接驱动一个 run;连接断开(刷新/关标页/抖动)即取消该 run(`async with RunSession` 退出 → 取消 task group)。v1 **不做**让 run 后台保活的 SessionManager。
- **身份**:AG-UI 的 `threadId` 即 Knuth 的 `run_id`,**ledger 是唯一存储**。首条消息(threadId 对应的 run 在 ledger 中不存在)→ `start(prompt, run_id=threadId)`(经决策 3 的可选 run id seam,用客户端首个 threadId 作为 run id);后续带 threadId → 按 ledger 中 run 状态路由到 `continue_run` 或 `resume`。
- **ID 约束(必须)**:一旦 client 的 threadId 进入 durable `run_id`,它会继续流入 `approval_id`(`approval_id_for` 直接拼 `appr_{run_id}_{tool_call_id}`,[invocations.py](../../packages/knuth-core/src/knuth/core/invocations.py))和 debug sink 文件名(`{run_id}.jsonl`,[debug.py](../../packages/knuth-runtime/src/knuth_runtime/debug.py))——后者意味着任意 client 串还是**路径注入**面。因此 `knuth-agui` **不接受任意 AG-UI/client 字符串作为 run id**:threadId 必须匹配服务端认可的 canonical 形态 `run_[A-Za-z0-9_-]{1,80}`,不符合直接 **400**,绝不透传进 `start(run_id=)`。(这也意味着前端要么用服务端发回的 run id 作为 threadId,要么自身生成符合该正则的 id。)
- **缺省 ID**:若请求没有 `threadId`,`knuth-agui` 生成 canonical `run_<uuid hex>` 作为 `threadId == run_id`,`RUN_STARTED` 同时返回 `threadId` 与 `runId`;前端必须持久化该 id,后续继续/恢复都带回同一个 `threadId`。
- 耦合省掉的是"跨断线保活正在执行的 run",省不掉"会话身份持久化":多轮对话与审批后续跑都靠 run_id 落库在多个独立 SSE 请求间续上。这由 ledger 天然提供,无需额外状态。

### 5. v1 工具范围:服务端工具 + 前端工具/生成式 UI

v1 同时包含服务端 CLI 工具(read/write/shell/search/glob)与前端工具(`useCopilotAction`)及其生成式 UI 渲染。这把最难的 seam(AG-UI client `ToolProvider` + 客户端工具回传)纳入 v1,因为"带生成式能力"是 knuth-im 的核心卖点,且直连拓扑(决策 2)要求 `knuth-agui` 本就要处理 `tools` 数组。

### 6. 客户端工具 seam:方案 A(Knuth 原生)✅

模型调用前端声明的工具时,run 必须挂起等浏览器执行完再回传。两种建模待实测后定:

- **方案 A(Knuth 原生,已选)**:前端工具经 `AGUIClientToolProvider` 进入 runtime `ToolRegistry`;agent loop 遇到这类工具不本地执行,而是把该 `ToolInvocation` 置入一个**新的中性等待态**等浏览器结果;前端 `POST /tool_result` → 追加 `ToolInvocationCompleted` → 再打开新的 `/agent` resume 流。客户端工具仍是一等 `ToolInvocation`,走完 batch/审计机制。
  这个等待态**必须是新概念,不能复用 `UnknownOutcome`**:`UnknownOutcome`/`UNKNOWN` 的语义是"外部写工具已开始、runtime 崩溃、中间结果不确定、需人工裁决"([CONTEXT.md](../../CONTEXT.md)),而客户端工具是"正常地、确定地在等浏览器执行",二者语义相反;当前 `ToolInvocationStatus` 也没有 waiting-client 态([invocations.py](../../packages/knuth-core/src/knuth/core/invocations.py))。需要新增中性状态,如 `WAITING_TOOL_RESULT` / `ToolInvocationAwaitingExternalResult`。可借的只是**机制**(像 `resolve_unknown` 那样"追加一个外部决定的完成事件来续 batch"),不是 crash recovery 的**语义**。`WAITING_APPROVAL` 暂停是另一个独立的机制先例。
- **方案 B(AG-UI 原生)**:发出客户端工具调用后结束本轮 run;前端执行后用 `continue_run` 携带 tool_result 消息开新一轮。不新增暂停态,但客户端工具调用绕过 Knuth 的 `ToolInvocation` 状态机/batch/审计,退化为消息往返。

实测后选择方案 A:它保持客户端工具的一等 `ToolInvocation` 审计、batch 关闭条件、history 重建与 resume 语义。方案 B 作为被拒绝替代方案保留在文末。

### 7. 审批/HITL:CUSTOM 事件 + /approve(仅解决) + 前端开 resume 流

服务端危险工具(`write`/`shell`,ADR-005 中 REQUIRES_APPROVAL)的人审,**两步,与 ADR-004"approval resolution 与 resume 是两步"一致**:

1. 命中 `WAITING_APPROVAL` → 本轮发 `RUN_FINISHED`、`WAITING_APPROVAL` 落库 → 前端从 AG-UI `CUSTOM` 事件渲染审批卡。
2. 用户点批准/拒绝 → `POST /approve {approvalId, decision}`:`knuth-agui` 只调 `runtime.approve/deny` **落库解决审批**,这一步**不返回 SSE、不自动续跑**。
3. `/approve` 返回后,**前端随即打开一条新的 `/agent` SSE `resume` 流**(threadId 不变)续跑。

对用户而言体验是连贯的("点批准 → agent 继续"),但架构上是"控制调用 + 新观察流"两步,`/approve` 与 `/agent` 各司其职。`PolicyEngine` 保持权威。

**不**让 SSE 连接挂着等人类点审批:耦合模型下连接一抖 run 就没了,人类审批是分钟级,必须靠落库 + 新连接 resume 而非长挂连接。

### 8. UI 壳:全屏 IM 式

独立 Next.js(App Router)应用,位于同仓 `apps/knuth-im-web`:左侧会话列表(`GET /threads` 列 runs,可切换、新建),主区 `CopilotChat`。切换会话时由 ledger 重建历史,以 `MESSAGES_SNAPSHOT` 注入。

### 9. UX:富渲染 + 少量生成式 UI(Claude Code 风)

聊天区把 agent 工作过程渲染出来:thinking 可折叠("thought for Xs");每个工具调用是实时状态卡(running → done/denied,参数/结果可展开);`shell`、file diff 等 1–2 个高价值工具用自定义 `render` 组件(生成式 UI),其余走通用卡片。语义照搬 knuth-cli 的 `EventRenderer`。

### 10. knuth-agui 端点面

- `POST /agent`:SSE run 流;按 threadId 与 ledger 状态决定 start/continue/resume。
- `POST /approve {approvalId, decision}`:解决审批(调用方随后触发 resume)。
- `POST /tool_result {runId, toolCallId, result}`:提交客户端工具结果;调用方随后打开新的 `/agent` resume 流。
- `POST /pause {runId}`:暂停当前 invocation,可 resume = `pause(run_id)` → `PAUSED`(比靠断连更可控)。**不叫 `/cancel`**:Knuth 区分 `PAUSED` 与 `CANCELLED`([types.py](../../packages/knuth-core/src/knuth/core/types.py)),而 runtime 目前没有 public `cancel()`(只有 `pause`,[agent.py](../../packages/knuth-runtime/src/knuth_runtime/agent.py));叫 `/cancel` 却进 `PAUSED` 会把 UI/协议/领域语言混掉。UI 上的"停止"按钮文案归 UI,语义统一映射到 pause。真正的 `CANCELLED`(`run.cancelled`)留到 runtime 出 public `cancel()` 再加,不在 v1。
- `GET /threads`:列会话(runs)。
- 历史:切会话时以 `MESSAGES_SNAPSHOT` 回放(读 ledger)。

### 11. 已定默认值

工具参数 token 级流式留作增强(v1 仍由 durable `tool.batch_planned` 全量发出,正确即可);协议编码改用官方 `ag-ui` SDK;前端框架 Next.js + CopilotKit + `@ag-ui/client`。

## 后果

- 新增包 `knuth-agui` 收敛为 transport 库:它导出 `create_app(runtime, client_tool_provider=...)` 和 AG-UI client `ToolProvider`,但 runtime factory 归 knuth-im host(M2/M3 把 spike 的 `build_spike_runtime` 迁出 transport 包)。新增前端应用 `apps/knuth-im-web`,仓库自此混 Python(uv workspace)与 JS 两套工具链。
- run id 不再是纯内部生成值:`knuth-agui` 必须在入口校验 canonical 形态(`run_[A-Za-z0-9_-]{1,80}`,400 拒绝),因为 run id 会流入 `approval_id` 与 debug 文件名(后者是路径注入面)。
- runtime 将获得几个加性 seam(`start(prompt, run_id=None)` 透传到 `ledger.create_run`、客户端工具的新中性等待态 + 补录),每个都与受众无关,可被 CLI/daemon 同样使用;runtime 不出现任何 HTTP/AG-UI 字样。工具来源仍统一为 runtime-wide `ToolRegistry`;没有 invocation overlay。
- 耦合会话模型意味着刷新/断线会取消正在执行的 run;这是 v1 已知接受的后果。若将来长 run 后台保活成为真实需求,再引入 SessionManager(run 后台驱动 + SSE 仅观察 + 重连重放),与 ledger 的事件溯源天然契合。
- `threadId == run_id` 让会话状态零额外存储,但要求 `knuth-agui` 用客户端首个 threadId 作为 run id 建 run;这是决策 3 的第一例 seam。
- 审批/客户端工具都走"落库 → 新 SSE resume",前端需编排两步(发起控制调用 → 开 resume 流),与 ADR-004"approval resolution 与 resume 是两步"一致。
- 客户端工具采用方案 A 后,M4 的核心协议 seam 已落入 runtime/transport/frontend 三层;更丰富的生成式组件可在不改协议的前提下继续扩展。代价是 AG-UI client tools 是 host/app 级能力集合,不是每个 invocation 的临时能力集合;若未来需要真正的 per-view 动态工具,应引入更明确的 session/capability profile,而不是恢复 `overlay_providers`。

## 修订里程碑

- **M1** ✅ spike:SSE + 事件翻译 + 真实模型验证(已完成)。
- **M2** ✅ AG-UI 完整化 + 会话身份:`start(prompt, run_id=None)`、threadId==run_id + canonical id 校验(400)、continue/resume 路由、`/threads` + 历史、`/pause`、换官方 encoder;最小 Next.js 会话 UI。→ 首个可用 Web 聊天。
- **M3** ✅ 服务端工具 + 审批:host 接 `create_cli_tool_provider` + policy、CUSTOM 审批 + `/approve`(仅落库解决)+ 前端开 resume 流、富渲染。
- **M4** ✅ 前端工具协议:方案 A 已定并实现 AG-UI client `ToolProvider` + 客户端工具新等待态 + `/tool_result` + 前端自动 resume。生成式组件继续作为 UI 增强迭代。

## 考虑过的替代方案

### 经 Node CopilotRuntime 代理到 knuth-agui

拒绝(本期)。标准 CopilotKit 拓扑是浏览器 → Node CopilotRuntime → AG-UI → agent,生成式 UI 与多 agent 路由开箱即用。但单用户本地 dev 助手不需要这层编排,多一个本地 Node 进程与一套 JS runtime 维护成本;直连更瘦。代价是 `knuth-agui` 要自己实现 AG-UI 完整面——已接受。

### 解耦会话:run 后台保活,SSE 仅观察,重连重放

拒绝(本期)。这是更健壮的模型(刷新不杀 run),也与事件溯源契合,但需要一个拥有长生命 `RunSession` 的 SessionManager、重连时先回放 durable 事件再挂实时 listener 的去重逻辑。v1 选耦合换简单;留作 run 真正变长时的演进。

### knuth-agui 自持 thread → run_id 映射表

拒绝。多一层间接,允许一个 thread 挂多 run 或存会话元数据,但 v1 不需要;`threadId == run_id` + ledger 更瘦,零额外状态。

### 客户端工具走"结束本轮 + continue 携 tool_result"(方案 B)

拒绝。它更 AG-UI 原生、runtime 改动更小,但让客户端工具调用绕过 `ToolInvocation` 状态机与审计,并把 tool result 退化成普通消息往返。M4 实测选择方案 A,以保留 batch/审计/history/resume 的一致性。

### 审批时长挂 SSE 连接,等人类决策后原地续流

拒绝。耦合模型下连接抖动即取消 run,人类审批是分钟级,长挂连接会因断线丢失审批中的 run。改为落库 `WAITING_APPROVAL` + 新连接 resume。

### v1 自动批准全部工具(allow-all)或禁掉危险工具

拒绝。allow-all 让本地 shell/write 无拦截执行,有误删/误改风险;禁掉危险工具又使 dev 助手能力缩水。审批 UI 性价比高(CUSTOM 事件已在 spike 中发出),纳入 v1。
