# Resume / 中断 控制路径缺陷汇总

> 来源：一次 CLI 交互中 Ctrl-C → `knuth approve` → `knuth resume` 的复现，暴露出两个表面无关、根因同源的问题。本文汇总该次讨论中定位到的全部问题、共同设计病灶，以及第一个问题（overlay 去除）的设计结论。

> **状态（2026-06-15）**：**P1 已修复**——`execute()` 现在自满足 registry 可用性，且 `overlay_providers` 通道已完全删除，agui client tool 改为注册式 provider。P2–P6 仍待处理。详见各节与文末「修复优先级」。

## 复现 trace

```
knuth ❯ list five top consuming procs
● shell({...})
  ⚠ approval required  ^C⊘ interrupted
run ... · waiting_approval

→ uv run knuth resume <run>
error: pending approvals must be resolved before resuming: <appr>

→ uv run knuth approve <appr>     # approved
→ uv run knuth resume <run>
  ✘ shell — Tool error: Tool not found: shell      # 症状 1
● python({...})
  ⚠ approval required  ^C
Interrupted.
^C Exception ignored in: <module 'threading' ...>   # 症状 2：第二次 ^C 才退出
KeyboardInterrupt:
```

两个症状：(1) resume 后第一个已批准的工具 `Tool not found: shell`；(2) 中断后进程卡死，需第二次 Ctrl-C。

---

## 问题清单

### P1 — resume 后 `Tool not found: shell`（双层根因）✅ 已修复

**第一层：registry 索引在新进程里未填充，而 execute 不会触发填充。**

- `ToolRegistry._manifest_index` 只有 `refresh()` 会填充，`add_provider` 只 `clear()` 不重建（[registry.py:26,28-44](packages/knuth-toold/src/knuth_toold/registry.py:26)）。
- `refresh()` 当时只在 `ToolBroker.propose()` / `list_visible_tools()` 里调用；`execute()` 直接查索引。
- `knuth resume` 是新进程，索引为空。run loop 进来发现 open batch 里是 **APPROVED** 的 shell，直接走 `_drive_open_batch` —— APPROVED 跳过 propose（状态机的正确行为，审批结论来自 ledger），于是在任何 `_run_step` / context build（会 refresh）之前就 execute → 空索引 → `Tool not found: shell`。
- 下一个 batch 的 `python` 走 propose → refresh，因此能找到。trace 完全吻合。

**修复**：`ToolBroker.execute()` 开头现在 `await self.registry.refresh()`（[broker.py:134-135](packages/knuth-toold/src/knuth_toold/broker.py:134)）。至此 `propose` / `list_visible_tools` / `execute` 三个 broker operation 都自满足 registry 可用性——execute 不再依赖 propose 的副作用。

**第二层：per-call overlay 工具根本不在 durable 状态里，refresh 也救不了。**

- broker 曾有第二条工具通道 `overlay_providers`，由 `start/resume` 入参经 `RunSession` → `RuntimeInvocation.tool_providers` 流入 `propose`/`execute`。
- 唯一真实消费者是 `knuth-agui`：每次 `/agent` 请求从 `body["tools"]` 构造 `ClientToolProvider` 作为 per-call overlay。
- 这些 provider 是纯进程内、不进 ledger 的。若一个用 overlay 工具的 run 被批准后从没有重新供给同一批 overlay 的路径 resume，execute fallback 到 registry → 真正的 `tool_not_found`，**且 refresh 救不了，因为该工具从未进过 registry。**
- CLI 能恢复纯属侥幸：CLI 工具是 build 时 `add_provider` 进 registry 的，每个进程 build 时都重建，所以 resume 进程里 shell 仍在 registry —— 只差索引未填充。

**修复**：`overlay_providers` 通道整条删除（broker / `AgentRuntime.{start,continue_run,resume}` / `RunSession` / `RuntimeInvocation` / `ContextBuilder` / `loop`）。agui client tool 改为**注册式 provider**，详见下文「已实施的设计」。

**设计问题（已解决）**：`execute()` 曾暗含前置条件「同进程已 propose/list 过」，与「APPROVED 可跳过 propose」的状态机自相矛盾，单进程测试测不出；per-run 的工具集合曾活在进程内存而非 registry/ledger，违反 resume 的基本假设。两者均已修正。

---

### P2 — Ctrl-C 后进程卡死，需第二次 ^C

**根因**：被放弃的 anyio worker 线程永久阻塞在一个永不会被 set 的 Event 上，而 anyio WorkerThread 不是 daemon，扣住了解释器退出。

- 审批/输入读取走 `_read_line` → `anyio.to_thread.run_sync(request.done.wait, abandon_on_cancel=True)`（[repl.py:476](packages/knuth-cli/src/knuth_cli/repl.py:476)）：一个 anyio worker 线程**无超时**阻塞在 `Event.wait()` 上。
- Ctrl-C 时 `abandon_on_cancel=True` 只放弃上层 await，`except BaseException` 把 request 标记 `abandoned`（[repl.py:479-481](packages/knuth-cli/src/knuth_cli/repl.py:479)）。
- 真正的 stdin reader 是 daemon 线程，阻塞在 `readline()` 不影响退出（[repl.py:424](packages/knuth-cli/src/knuth_cli/repl.py:424)）；但它对 abandoned request 只 `continue`、**从不 resolve**（[repl.py:432-436](packages/knuth-cli/src/knuth_cli/repl.py:432)），`done` 永不 set。
- anyio WorkerThread 继承主线程 → **非 daemon**。`threading._shutdown` join 它时永久阻塞，第二次 ^C 才打断 → "Exception ignored in threading._shutdown"。

**注意**：仅在 reader loop 里对 abandoned request `resolve()` **不够**——Ctrl-C 后没有新输入进来，reader 自己仍阻塞在 `readline()`，`done` 仍不会 set。真正的修法是**不要把非 daemon worker 停在无界等待上**：让已有的 daemon reader 通过 `loop.call_soon_threadsafe` 去 set 一个 `anyio.Event`，`_read_line` 直接 `await event.wait()`（可取消、不占线程）；或把等待做成可感知 abandon 的有界轮询。

**设计问题**：abandonment 协议只做了一半——丢弃迟到的输入行，但没释放等待者，把线程释放绑在一个设计上不会发生的事件上。

---

### P3 — `run_resume` 缺中断/pause 路径，中断被误判为崩溃

- 交互式 turn 用 `open_signal_receiver` + CancelScope + pause 的完整机制（`_run_turn_interruptible` / `_pause_after_interrupt`）。
- 但 `run_resume`（[repl.py:118](packages/knuth-cli/src/knuth_cli/repl.py:118)）裸跑 `await session.result()`，无信号处理、无 pause。Ctrl-C 发生在工具执行中会把 run 留在 RUNNING；下次 resume 走 crash-recovery（[loop.py 崩溃恢复段](packages/knuth-runtime/src/knuth_runtime/loop.py)），把「用户主动中断」误判成「运行时崩溃」。

**设计问题**：「驱动一个 run 并渲染」这一职责有 `run_single` / `run_interactive` / `run_resume` 多套不完整实现，中断语义只在其中一套里实现。

---

### P4 — `_resolve_approvals` 的 `except KeyboardInterrupt` 是死代码

- asyncio Runner 把 SIGINT 转成主 task 的 `CancelledError`，`KeyboardInterrupt` 只会从 `anyio.run` 顶层抛出。
- 因此 `_resolve_approvals` 里 `except KeyboardInterrupt: answer = None` 永不命中，`_print_approval_handoff` 的提示永远不打印（trace 里也确实没有）。

**设计问题**：中断处理混用了四套互相矛盾的策略（signal receiver / except KI / 裸跑 / main 兜底），没有统一语义。

---

### P5 — 被放弃的输入会吞掉之后的第一行

- Ctrl-C 时内核已清空输入缓冲；reader 仍阻塞在 `readline()`。它后续 swallow 的是**未来用户输入的一行**，而非被打断的那行（[repl.py:432-436](packages/knuth-cli/src/knuth_cli/repl.py:432)）。
- 与 P2 同源；统一 input driver 后一并消除。

---

### P6 — 审批只冻结 effect/risk，未冻结 manifest 指纹（审计漂移）

- `ToolManifest` 只有 `provider` 字段，无 version/hash（[base.py:16-28](packages/knuth-toold/src/knuth_toold/base.py:16)）。
- 审批冻结了 args_hash / effect / risk / execution_mode（写在 invocation / `ApprovalRequestedDraft` 上），但**未记录批准的是哪个 manifest/provider 版本**。
- 真正执行的 manifest（cwd、timeout、甚至背后 provider）是晚绑定的。对 builtin 漂移风险低；对 overlay / plugin provider 是真实的 TOCTOU 审计缺口。

**修法（长期、可选）**：propose 时把 manifest fingerprint 记入 `tool.proposed`，execute 前校验漂移；或接受晚绑定并在文档中明确。

---

## 共同病灶

**持久层（ledger）已按「跨进程、可中断、可恢复」建好，但若干进程内资产仍停在「单进程跑完一个 run」的心智模型里**：

| 资产 | 原现状 | 应当 | 状态 |
|---|---|---|---|
| 工具索引 `_manifest_index` | 进程内惰性缓存，execute 前不保证已填充 | 每个 broker operation 自满足可用性 | ✅ execute 现也 refresh |
| per-call overlay 工具 | 活在 call 参数 / 进程内存 | 工具世界是 run 的 registry/durable 属性，或固定契约 | ✅ 通道删除，改注册式 provider（见残留风险） |
| stdin 读线程 | 非 daemon worker 停在无界等待 | 统一 input driver，可取消、不泄漏线程 | ⬜ P2 待处理 |
| 中断策略 | 四套并存、部分是死代码 | 中断是一等运行时事件，所有 run 入口共享一条路径 | ⬜ P3/P4 待处理 |

resume 那条已收口；interrupt 那一组（P2–P5）仍待统一。

---

## 已实施的设计：去掉 `overlay_providers`

### 为什么能删

- 唯一真实消费者是 `knuth-agui`，前端每请求经 `body["tools"]` 声明、由客户端执行的工具。CLI / im 传的 `tool_providers` 是给 `build_sqlite_runtime`（build 时进 registry，durable），不是 per-call——删掉对它们零影响。
- **overlay 通道与 `execution_mode` 冗余**：运行时是否在进程内执行一个工具，看 `inv.execution_mode`（[loop.py:414-417](packages/knuth-runtime/src/knuth_runtime/loop.py:414)）——这是 propose 时冻结到 invocation 上的 durable 事实，与 provider 来自哪个通道无关。client tool 是 `ExecutionMode.EXTERNAL`：loop 永不 execute 它，而是进入 `WAITING_TOOL_RESULT` 等 `/tool_result`，`call_tool` 永远报错是死路径。
- 所以那个「活」的 provider 对象对 EXTERNAL 工具的路由/执行毫无必要；它唯一不可替代的职责是每个 step 把「本 run 可见的 client tool manifests」广播给模型（`list_visible_tools`）。overlay 真正承载的是「可见工具集合」，原罪与 P1 同源——这份状态活在进程内存而非 registry。

### 实际落地（注册式可变 provider）

采用了介于「静态注册」与「持久化」之间的务实方案：

- `ClientToolProvider` → `AGUIClientToolProvider`（[client_tools.py](packages/knuth-agui/src/knuth_agui/client_tools.py)）：一个**进程级、可变、带锁**的 provider，`list_tools()` 返回累积的 manifests。
- 进程启动时 `create_agui_client_tool_provider()` 创建单例，经 `build_runtime(tool_providers=[client_tool_provider])` **注册进 registry**，并同一实例传给 `create_app(..., client_tool_provider=...)`（[__main__.py:107-114](packages/knuth-im/src/knuth_im/__main__.py:107)）。
- 每次 `/agent` 请求若带 `body["tools"]`，先 `client_tool_provider.register_agui_tools(tools)` 把声明**累积合并**进单例（[app.py:244-254](packages/knuth-agui/src/knuth_agui/app.py:244)），再 `_session_factory` 起会话。
- 同名工具若以不同 fingerprint 重复注册会抛 `ValueError`（[client_tools.py `_register`/`_fingerprint`](packages/knuth-agui/src/knuth_agui/client_tools.py)），防止静默漂移。
- `ExecutionMode.EXTERNAL` 保留，routing 不变，结果仍走 `/tool_result`。
- 所有 per-call `overlay_providers` / `tool_providers` 形参整条删除；只剩 build 时注册。

**效果**：client tool 现在进了 registry，配合 P1 的 `execute()` refresh，同进程内声明过的工具在该进程的任何 resume 都可见。`list_visible_tools(run_id)` 不再需要 per-call 注入。

### 残留风险（这是 A/B 折中的边界，非 bug）

注册式单例**不是 ledger 级持久**，工具世界的恢复仍依赖前端在每次 `/agent` 重新声明：

1. **进程重启 / 跨表面 resume**：单例在内存里，im 重启或用 `knuth resume`（CLI runtime 无此 provider）拉起一个等待 client tool 的 run，工具会缺失。自愈条件是「下一次 `/agent` 由前端重新声明同一批工具」（agui 在起会话前会 re-register），否则退化回 P1 第二层那类 `tool_not_found`。彻底消除需把声明写进 ledger（原方案 B）。
2. **跨 run 串味 + 无界累积**：单例对所有 run 共享，`list_visible_tools` 忽略 run_id，**累积的全部 client tool 对所有 run 可见**，且进程生命周期内只增不减。同构前端（同一应用版本同一套工具）下无碍；多前端 / 多版本并发时，run A 会看到 run B 声明的工具。
3. `/approve`、`/tool_result`、`/pause` 不重新注册——但它们不驱动 loop，真正推进发生在下一次 `/agent`，故不额外引入问题。

**若未来需要严格 per-run 隔离或跨进程持久**，再升级为 ledger-backed：首次声明时把 manifests 作为 run 状态写入 ledger（如 `ClientToolsDeclaredDraft`），由 run-scoped 路径重建可见工具。当前折中对单一同构前端已足够。

---

## 修复优先级

1. ✅ **P1 第一层**（refresh 可用性）：`execute()` 现也 refresh。
2. ✅ **overlay 去除**：通道删除，agui 改注册式 `AGUIClientToolProvider`（残留风险见上，单一同构前端下足够）。
3. ⬜ **P2 + P3 + P4 + P5**（统一前台 input/中断 driver）：一并解决卡死、中断误判、死代码、吞行——当前最高优先。
4. ⬜ **P6**（manifest fingerprint）：审计强化，可后置。
5. ⬜ **（可选）overlay 升级 ledger-backed**：仅当需要严格 per-run 隔离或跨进程持久时。
