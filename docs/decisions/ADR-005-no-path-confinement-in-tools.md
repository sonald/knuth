# ADR-005: 内置工具零路径策略

## 状态
Accepted

## 日期
2026-06-12

## 背景

`_ExecutionContextTool`（knuth-toold `builtins.py` 与 knuth-cli `tools/files.py` 各有一份）让所有内置文件工具和子进程工具在构造时持有一个 `cwd`，并在路径解析时强制 `path.is_relative_to(execution_dir)`，越出该目录即抛 "path must stay within the execution directory"。

追溯来历：这个约束最早出现在 v0 初始实现 commit（`df1d5dc Implement Knuth v0 architecture`）中；`docs/knuth-v0-design.md` §9.2 仅有一句"路径沙箱统一用 `Path.is_relative_to`（现实现已正确），禁止 `str.startswith`"——讨论的是实现手法（防 `startswith` 前缀漏洞），从未论证"是否应当限制工具的访问范围"；`docs/decisions/` 中没有任何 ADR 记录这个决定。它是实现细节固化成的隐式全局行为，不是被审视过的设计。

这个设计实际上把两个框架不该有的意见塞进了工具实现：

1. **圈禁**：工具拒绝执行目录之外的路径——框架替所有 agent 预设了一个最严格且无法解除的访问边界。
2. **路径解释权**：工具构造时接收 `cwd`，把"相对路径相对什么解析"变成了每个工具实例的配置——框架发明了一套自己的路径语义。

Knuth 是通用 agent 框架。"什么样的路径是合理的"不是框架的事情：本地 shell agent 要读家目录配置和系统文件；受控 agent 要圈禁到 workspace；CI agent 可能只读。这些答案全部因 agent 而异，属于各 agent 的策略。框架已有表达这类裁决的位置：`PolicyEngine`（knuth-runtime `policy.py`）在 propose 阶段基于 manifest facts + args 做纯函数裁决（ALLOWED / REQUIRES_APPROVAL / DENIED），裁决进 ledger 可审计。

## 决策

**内置工具对路径零策略：不圈禁，也不持有路径配置。**

- **删除圈禁。** 去掉 `_execution_path` 中的 `is_relative_to` 校验和 "path must stay within the execution directory" 错误。
- **删除工具的 `cwd` 构造参数。** `ReadFileTool` / `WriteFileTool` / `EditFileTool` / `ShellTool` / `PythonTool` 构造时不再接收 `cwd`，`create_default_registry(cwd)` 的 `cwd` 参数同步删除。`_ExecutionContextTool` 基类随之消亡——剩余的路径参数类型校验下沉到各工具自身。
- **路径语义就是操作系统语义。** 绝对路径原样使用；相对路径相对进程 cwd 解析——这不是框架行为，而是 `open()` 和子进程继承 cwd 的 OS 默认行为，框架不做任何解释、规范化或重定向。子进程工具（`shell` / `python`）继承进程 cwd，需要别的目录时模型自己 `cd`。
- **访问控制（若某个 agent 需要）属于策略层。** 在 `PolicyEngine.evaluate_tool_call` 基于 args 中的 path 做裁决，或由 agent 注册自己的 policy；manifest 已携带 effect/risk facts。v0 不预先实现任何路径策略——等有真实需求的 agent 出现时再加。
- **工具 manifest description 与 CLI system prompt 同步去掉 "execution directory" 表述**，不再暗示存在访问边界；工具结果中显示模型传入的路径原文，不做相对化渲染。

## 后果

- 工具实现变薄：路径处理只剩参数类型校验；两份重复的基类同时消失；工具实例变成无位置状态的纯能力对象，注册表的构造不再需要知道 agent 的目录布局。
- 对 knuth-cli 行为无变化：CLI agent 的进程 cwd 就是用户当前目录，工具此前默认 `Path.cwd()`，去掉参数后语义相同。
- 模型可以读取任意路径而不经审批：`read_file` 是 LOW/READ，policy 现状直接放行。这是已知且接受的后果——对本地 shell agent 这正是期望行为；若未来某个 agent 需要限制读取范围，在它的 policy 里做，而不是改工具。
- 写操作的风险面没有放大：`write_file` / `edit_file`（LOCAL_WRITE）和 `shell` / `python`（DANGEROUS）依旧 REQUIRES_APPROVAL，审批时可见具体路径/命令。
- 测试不再以 `ReadFileTool(tmp_path)` 的形式注入临时目录，改用绝对路径指向临时文件。
- 若未来出现单进程承载多个 run、且各 run 需要不同工作目录的宿主（daemon），届时 cwd 是按 invocation 注入的执行事实，应经由 `ToolRuntimeContext` 进入，而不是恢复工具构造参数。
- `docs/knuth-v0-design.md` §9.2 "路径沙箱"一条作废，由本 ADR 取代。

## 考虑过的替代方案

### 保留圈禁，允许配置多个 allowed roots

拒绝。仍是把访问策略嵌进工具实现，只是从一个 root 变成 N 个 root。配置面长在错误的层上：policy 与工具各有一套访问规则，ledger 里的 PolicyDecision 与工具实际执行的限制可能不一致。

### 去掉圈禁，但保留 `cwd` 构造参数作为相对路径解析基准

拒绝。这是本 ADR 第一稿的方案。它消除了圈禁，但保留了框架自己的路径语义——"相对路径相对工具实例的 cwd 解析"仍是框架发明的解释规则，工具实例因此带有位置状态，注册表构造仍需感知 agent 目录布局。路径相对什么解析是 OS 已经定义好的事情，框架重复定义没有收益。

### 把圈禁做成基类开关（构造参数 confine=True/False）

拒绝。开关意味着同一份安全语义有两个家。裁决入口必须唯一（PolicyEngine），否则"为什么这个调用被拒绝"没有单一事实来源。

### 强制模型只用绝对路径

拒绝。相对路径相对进程 cwd 解析是所有命令行程序的共同语义，模型熟悉它；禁用徒增模型负担和出错率，且依然是框架在对路径形态发表意见。
