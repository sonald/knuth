# Interactive Commands 设计说明

状态：Proposed
日期：2026-06-24
依据：[CONTEXT.md](../CONTEXT.md)、[Skills 内置能力需求与设计](skills-requirements-and-design.md)、[ADR-004](decisions/ADR-004-runtime-control-and-agent-loop-boundaries.md)、[ADR-006](decisions/ADR-006-knuth-im-agui-transport.md)

本文记录 Knuth 交互命令的 v1 设计约束。它不是 ADR；这里只固定实现前需要共享的术语、范围和行为规则。

## 目标

- 让 CLI slash 输入、IM slash 输入、IM generated UI 复用同一套 `InteractiveCommand` 语义。
- 让 help、completion、dispatch 从同一个 command catalog 派生，避免多份硬编码列表漂移。
- 让 skill 默认作为 command 暴露，同时继续复用现有 `skill` tool 执行路径。
- 增加 `/usage [run_id]`，展示指定 run 或当前 run 的模型 token 用量。
- 保持 runtime 边界干净：runtime 不拥有 command catalog，也不理解 slash syntax。

## 非目标

- 不设计通用 extension command ABI。
- 不设计 `CommandResult` 或 action/result DSL。
- 不新增通用 `/commands/invoke` endpoint。
- 不做 command argument schema；v1 只传递 command token 后的原始字符串。
- 不把 command invocation 写入 durable runtime history。
- 不实现账户 quota、全局 token 统计或本地 token 估算。

## 领域边界

`InteractiveCommand` 是 host-owned 的交互动作。它发生在用户文本进入模型 conversation 之前，不是 runtime control，也不是 durable event。

`CommandInvocation` 是 host-local 的解析结果，只包含：

```text
name
raw_args
surface
```

`raw_args` 是 command token 后面的完整剩余字符串，保留换行。command core 不解析参数；具体 command handler 自己解释 `raw_args`。

`CommandSurface` 是触发 command 的交互面，例如：

```text
cli.slash
im.slash
im.generated_ui
im.command_palette
```

`CommandCapability` 是 host 暴露给 handler 的窄能力集合。handler 不接收裸 runtime、console、React app、FastAPI app 或 transport 对象。

## 分层

### knuth-core

`knuth-core` 可以放极薄的共享 command 数据与解析工具：

```text
CommandSpec
CommandInvocation
CommandSurface
parse_slash_invocation(text, catalog)
build_command_catalog(builtin_specs, skill_infos)
project_skill_commands(skill_infos, reserved_names)
```

core 不包含：

```text
handler
CommandContext
runtime call
UI render
SkillManager access
/usage aggregation
```

### Host packages

CLI 和 IM host 各自拥有 command handlers 与 capability context。

CLI 示例能力：

```text
ui.print(...)
conversation.start_or_continue(prompt)
conversation.resume(run_id)
runtime_read.events(run_id)
runtime_read.skills()
runtime_read.tools()
```

IM 示例能力：

```text
ui.toast(...)
ui.render_panel(...)
conversation.stream_agent(prompt)
conversation.resume(thread_id)
runtime_read APIs exposed through existing or narrow HTTP endpoints
```

### Runtime

runtime 不拥有 command catalog，不解析 slash command，不持久化 command invocation。

runtime 可以暴露窄 read API，例如：

```text
runtime.skills() -> list[SkillInfo]
```

该 API 只暴露 runtime 已有的 skill catalog 信息，供 host 构建 command catalog；它不代表 runtime 拥有 command 语义。

## Catalog

`CommandCatalog` 是按需构建的 host-side view：

```text
builtin command specs + current runtime.skills()
```

它不是状态源，不维护 snapshot/version/dirty flag，不读取 skill 文件系统，也不拥有 hot reload。

排序规则：

```text
builtin: 按定义顺序
skills: 按 skill name 字典序
display: 按 source 分组
```

冲突规则：

```text
builtin wins
/skill:<name> always available for existing skill
/<name> available only when it does not conflict with builtin commands
```

用户可见 usage 主要展示 canonical `/skill:<name>`，`/<name>` 只是便利 alias，可以参与补全，但不作为主文档形式。

## Slash Parsing

Slash invocation 只在提交文本 trim-left 后以 `/` 开头时尝试识别。

解析规则：

```text
token = / 后直到第一个 whitespace 的字符串
raw_args = token 后完整剩余文本，保留换行
```

不支持 quoted command names，不支持 escaped whitespace。command token 不能包含 whitespace。

解析函数语义：

```text
parse_slash_invocation(text, catalog) -> CommandInvocation | None
```

`None` 表示 ordinary prompt path，包括：

```text
text 不以 / 开头
leading slash token 不在当前 catalog 中
```

known command 一旦被解析出来，就由 handler 拥有后续行为。参数错误或执行错误是 host-level command error，不回退为普通 prompt。

## Skill Commands

Skill 默认投影成 command。

稳定形式：

```text
/skill:<skill-name> <raw_args>
```

便利形式：

```text
/<skill-name> <raw_args>
```

仅在不和 builtin command 冲突时可用。

fallback builtin：

```text
/skill <skill-name> <raw_args>
```

行为差异：

```text
/skill:missing
  unknown dynamic slash token -> ordinary prompt

/skill missing
  known builtin /skill -> host-level skill-not-found error
```

`SkillCommand` 不直接读取 `SKILL.md`、不调用 `SkillToolProvider.call_tool(...)`、不伪造 tool result。它只把 invocation 编译成 model-visible user turn。

初始模板：

```text
Use the `<skill_name>` skill for this request before answering.

Skill command arguments:
<raw_args>
```

如果 `raw_args` 为空：

```text
Use the `<skill_name>` skill for this request before answering.
```

SkillCommand 编译后的 user turn 走普通 prompt 的 start/continue 规则：

```text
有 current run -> continue_run(current_run_id, compiled_prompt)
没有 current run -> start(compiled_prompt)
```

CLI prompt history 保存用户原始 slash 输入；runtime durable conversation 保存编译后的 model-visible user message。

## Builtin Commands

v1 保留当前已有 commands，并新增 skill fallback 与 usage：

```text
/help
/tools
/new
/clear
/resume
/status
/skill
/usage
/exit
/quit
```

暂缓：

```text
/runs
/reload
/cancel
/commands
```

这些行为可以后续增加，但不属于 command 架构 v1 的必要范围。

## Token Usage

`/usage [run_id]` 展示 per-run model token usage。

范围：

```text
/usage
  使用当前 session_run_id

/usage <run_id>
  使用指定 run
```

如果没有 current run：

```text
No active run. Use /usage <run_id>.
```

数据来源：

```text
durable model.completed.usage
```

聚合字段：

```text
model calls with usage
input_tokens
output_tokens
total_tokens
cost_usd, if present
```

不做本地 token 估算。provider 未返回的字段显示 unavailable。

`RUNNING` run 只展示已经完成并写入 ledger 的 `model.completed.usage`，不等待 live generation 结束。

第一版 `/usage` 聚合放 host command handler，直接读取 `runtime.events(run_id)` 并筛选 `model.completed`，不新增 runtime usage API。

## IM Surface

IM 可以支持 slash text，也可以支持 generated UI。generated UI 是 `CommandSurface`，不是新的 command 类型。

IM command discovery 使用 host-level endpoint：

```text
GET /commands
```

它返回展示与解析 metadata：

```text
name
source
description
canonical
```

不返回：

```text
handler type
capability list
runtime method name
action/result protocol
compiled prompt template
```

第一版不做通用 `POST /commands/invoke`。IM 前端解析 invocation，并用既有或窄 backend APIs 执行对应 capability。

`/agent` endpoint 继续只接收模型可见 messages 或 resume 请求，不解析 raw command invocation。

## 错误与持久化

CommandInvocation 默认不持久化。

规则：

```text
Pre-runtime command errors:
  host-local, not durable

Runtime effects caused by a command capability:
  durable according to the called runtime API's existing semantics

Model-visible turns compiled by commands:
  durable as ordinary user messages
```

示例：

```text
/status
  host read-only; not durable

/usage missing-run
  host error; not durable

/skill:writer args
  compiled user message; durable as ordinary user message

model failure after SkillCommand starts a turn
  durable runtime failure per existing run semantics
```

## 验收清单

- help、completion、dispatch 从同一份 catalog 派生。
- unknown leading slash token 作为 ordinary prompt。
- known command 的参数错误不回退 prompt。
- `/skill:<name>` 和无冲突 `/<name>` 能启动/继续普通 run。
- `/skill <missing>` 报 host-level skill-not-found。
- SkillCommand 不直接加载 skill 内容或伪造 tool result。
- CLI prompt history 记录 raw slash input；durable conversation 记录 compiled prompt。
- `/usage [run_id]` 聚合 durable `model.completed.usage`，不估算 token。
- builtin 与 skill command 冲突时 builtin wins，skill 仍可用 `/skill:<name>`。
- IM `/commands` 只返回 metadata，不返回 handler/action 描述。
