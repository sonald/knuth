# PRD: knuth-cli REPL 输入能力升级

## 状态

已实现

## 目标

把 `knuth run` 从一个裸 line reader 升级为真正可长期使用的交互式 agent shell。REPL 需要支持持久 prompt 历史、多行编辑、命令/工具补全、readline 风格 Emacs 按键，以及可预期的 Ctrl-C / approval 行为，同时保留 Knuth 现有 runtime/control 接口边界。

## 背景

当前 `knuth-cli` 在 `run_interactive(...)` 中通过 `_read_line(console, _PROMPT, preserve_late_line_on_cancel=True)` 读取顶层 prompt，然后把 slash command 或用户 prompt 路由到 runtime control。Approval prompt 也复用同一个 `_read_line(...)` helper。这个 helper 底层是自定义 `_StdinReader`：它用一个 daemon 线程串行执行 `stdin.readline()`，避免 Ctrl-C 后 abandoned reader 竞争 stdin，也避免 UTF-8 输入被多个 reader 撕裂。

这解决了之前的终端生命周期 bug，但输入体验仍然停留在 canonical line input：没有持久历史、没有多行编辑器、没有补全 UI，也没有一等 keymap。这个升级应该完全替换当前 stdin 输入实现，而不是在 `_StdinReader` 上继续兼容或扩展。需要延续的是 Ctrl-C 的中断语义，而不是旧输入机制本身。

目标输入体验应包含以下内嵌交互模型；PRD 自身就是完整规格：

- 上下文 keybinding：global、chat、autocomplete、history-search 分开；只有 overlay 激活时才由 overlay 拥有 `Esc`、`Tab` 和方向键。
- 文本编辑拥有常见 Emacs/readline 操作，例如 `Ctrl-A/E/B/F/K/U/W/Y`、`Meta-B/F/D`、通过 `Ctrl-P/N` 导航历史，以及按词移动/删除。
- 多行导航是 line-aware 的：Up/Down 先在输入内部移动，只有在第一行/最后一行边界才切换到历史导航。
- 历史是 append-only JSONL，带 project/session 元数据，按最新优先有界读取，搜索时去重。
- 历史搜索优先采用 prompt-toolkit 内置能力；只有 accept/cancel/execute 语义不满足 Knuth 硬验收时，才增加自定义模式。
- Autocomplete 是 overlay：`Tab` 接受，`Esc` 关闭，方向键只在建议可见时导航建议。

对 Knuth v1 来说，引入 Python 成熟输入库 `prompt_toolkit` 是最小、内聚的实现路径。官方文档说明 `PromptSession` 是面向重复 prompt 的对象，能在多次 prompt 调用之间保留 history；同时支持 completer、自定义 key binding、多行输入和 `EditingMode.EMACS`。它还提供 file history、threaded completion、auto suggestions、async prompting 和 prompt continuation hook。

参考：

- 当前 Knuth 顶层 prompt 路径：`packages/knuth-cli/src/knuth_cli/repl.py:127`
- 当前 approval prompt 路径：`packages/knuth-cli/src/knuth_cli/repl.py:430`
- 当前待替换的 `_StdinReader` 与 Ctrl-C race guard：`packages/knuth-cli/src/knuth_cli/repl.py:625`
- Runtime/CLI 边界：`docs/decisions/ADR-004-runtime-control-and-agent-loop-boundaries.md`
- Interrupt/reentry 边界：`docs/decisions/ADR-007-interrupt-signal-and-reentry.md`
- `prompt_toolkit` prompt session / history / completion 文档：https://python-prompt-toolkit.readthedocs.io/en/master/pages/asking_for_input.html
- `prompt_toolkit` key binding 文档：https://python-prompt-toolkit.readthedocs.io/en/master/pages/advanced_topics/key_bindings.html

## 范围

- 用 `knuth-cli` 拥有的新输入层完全替换当前 stdin 读取机制，包括顶层 prompt、approval prompt 和非 TTY stream 读取。
- 新输入 adapter 和测试迁移完成后，删除 `_StdinReader` / `_read_line(...)`；测试应迁移到新输入 adapter，而不是 patch 旧 helper。
- 在 `knuth-cli` 配置同族的 app data 目录下持久化 prompt 历史。
- 增加历史导航、反向历史搜索、多行编辑、slash command 补全、`/resume` / `/status` 的 run id 补全，以及 `/tools` / approval helper 适用场景下的 tool name 补全。
- 保留顶层 Ctrl-C 语义：空 prompt 下 Ctrl-C 留在 REPL，active turn 下 Ctrl-C interrupt foreground `RunSession`，approval 下 Ctrl-C 让 run 保持 `WAITING_APPROVAL`。
- 保持 `run_single(...)`、`knuth run --once`、脚本路径和 smoke test 的外部行为仍然是 line-oriented、非交互式路径；内部可以由新输入层提供 stream reader。

## 非目标

- 不把终端输入、历史或 keybinding ownership 移入 `knuth-runtime`。
- 不为了新输入能力修改 `AgentRuntime.start/continue_run/resume`、`RunSession`、runtime event schema、CLI 子命令参数或 durable run status。
- 不保留 `_StdinReader` / `_read_line(...)` 作为兼容层；旧输入逻辑可以被端到端替换。
- 本轮不做完整 Textual/Ink 主聊天 UI 替换。
- 暂不实现 Vim mode。第一版目标是 Emacs/readline 风格编辑。
- 除非能从 `prompt_toolkit` 自然得到且不引入新产品面，否则不加入 image paste、prompt stash、background-agent control 或 transcript mode。
- v1 不做 `Ctrl-X Ctrl-E` / `$EDITOR` external editor launch；内置多行编辑是 v1 的长 prompt 解法。
- 不修改 approval 语义或 AG-UI 行为。
- 补全不能在每次按键时触发模型调用或长耗时工具发现。

## 用户需求

### R1: 持久 Prompt 历史

- TTY REPL 在普通用户 prompt 成功离开编辑器后记录历史。
- v1 不记录 slash command 历史；slash command 只参与补全和执行。若未来要记录，应先定义哪些命令可安全重放。
- 空输入、纯空白输入、未通过校验的输入、`/exit`、`/quit`、临时 approval answer 不进入 prompt 历史。
- 非 TTY / pipe 输入不写入 prompt 历史。
- 历史跨 `knuth run` 会话持久存在。
- 历史按 `ProjectKey` 隔离，同时保留 `cwd` 等审计元数据。
- `/new` / `/clear` 重置 `session_run_id` 时，也开始新的 history `session_id`；history file 不变，默认导航和搜索仍按 `ProjectKey`，所以新 session 仍能找回同一 project 下的旧 prompt。
- 写入时折叠连续重复项；搜索/导航时按最新优先对相同文本去重。
- 用户向下导航越过最新历史项时，恢复当前未发送 draft。
- 历史读失败不能让 REPL 崩溃；只降级到内存历史，并最多在 debug 模式输出 warning。

### R2: 多行编辑

- 用户可以在终端编辑器里编写多行 prompt。
- 普通 Enter 提交。
- `Esc`+`Enter`、`Meta`+`Enter`、反斜杠后接 Enter 插入换行。这个行为匹配常见终端编辑器和 prompt-toolkit 的便携多行接受模型。
- 粘贴的多行文本保持为多行输入；提交前 tab 归一化为空格。
- Up/Down 先在多行 buffer 内移动。只有 cursor 位于第一行时 Up 才导航历史；只有 cursor 位于最后一行时 Down 才向前导航历史。
- 长输入可以视觉换行，并且不破坏 CJK 文本的 cursor 位置。

### R3: Emacs/Readline 编辑

- 默认编辑模式是 Emacs/readline。
- 必需按键：`Ctrl-A/E` 行首/行尾，`Ctrl-B/F` 左/右移动一个字符，`Meta-B/F` 左/右移动一个词，`Ctrl-K` kill 到行尾，`Ctrl-U` kill 到行首，`Ctrl-W` kill 前一个词，`Meta-D` 删除后一个词，`Ctrl-Y` yank，`Ctrl-P/N` 上/下一条历史，`Ctrl-L` 清屏/重绘，`Ctrl-D` 仅在空 buffer 下表示 EOF/退出。
- idle prompt 下 `Ctrl-C` 取消当前本地编辑、丢弃 draft，并重新显示干净 prompt；它不写历史、不产生 runtime event、不能退出整个 CLI。清空当前行使用 `Ctrl-U`。
- agent turn 运行时 `Ctrl-C` 保留现有 `_drive_session_to_result(...)` graceful interrupt 语义。
- v1 不要求支持经常被终端占用的 `Ctrl-S/Q` 组合。

### R4: 补全

- 当 cursor 位于 slash command token 时，提供 slash command 补全。
- 补全候选至少包含 `/help`、`/tools`、`/new`、`/clear`、`/resume`、`/status`、`/exit`、`/quit`。
- `/resume` 和 `/status` 在可用时补全最近 run id，并展示 status metadata。
- `/tools` 在用户输入第二个 token 时，可以补全 tool name 或 subcommand。
- 补全不能因为慢 runtime call 阻塞编辑器。候选 snapshot 后台刷新；keypress 路径只读取内存 snapshot。
- Completion overlay 只在可见时拥有 `Tab`、`Esc`、Up、Down。
- 默认不为自然语言 prompt 文本提供嘈杂的普通 word completion。

### R5: 历史搜索

- `Ctrl-R` 启动反向历史搜索。
- 输入 query 后匹配最新的历史项。
- 重复按 `Ctrl-R` 跳到更旧的匹配项。
- `Enter` 接受并提交匹配项。
- `Esc` 或 `Tab` 接受匹配项但不提交。
- `Ctrl-C` 取消搜索并恢复原 draft。
- 没有匹配项时，原 draft 保持完整。

### R6: 新输入层的非 TTY 路径

- 非 TTY stdin/stdout 保持现有外部行为：逐行读取、EOF 退出、空输入忽略、脚本路径不进入交互编辑器。
- 非 TTY 是兼容性的 batch-ish REPL，不启用历史、补全、多行编辑、autosuggest，也不写 prompt history。
- 非 TTY 普通文本每行作为一个 prompt 提交；slash command 仍按现有 REPL 命令执行。
- 非 TTY 下 Ctrl-C 只承诺进程级中断，不承诺 prompt-toolkit 的本地编辑取消语义。
- 非 TTY 读取由新输入层实现，不依赖 `_StdinReader`。
- 单元测试应通过注入 fake input adapter 或 fake stream 验证行为，不再依赖 patch `_read_line(...)`。
- TTY 下如果 prompt-toolkit 因环境不支持 raw-mode editing 或其它初始化错误而不可用，REPL 应报错退出；不做 simple stream 降级，也不能回退到旧 `_StdinReader`。

### R7: Approval Prompt 语义保持

- Approval prompt 保持简单且确定。
- Approval 输入可以由同一个 input adapter 负责，但必须使用独立的最小 prompt 配置：无历史、无补全、无 autosuggest、单行、只保留基础 Emacs 编辑。approval answer 不写入 prompt 历史。
- Approval 输入中 Ctrl-C 让 run 保持 `WAITING_APPROVAL`，并像今天一样打印 handoff commands。

## 技术设计

### 新模块：`knuth_cli.input`

引入 CLI-local 输入 adapter 层：

```python
@dataclass(frozen=True)
class InputResult:
    kind: Literal["text", "cancelled", "eof"]
    text: str = ""


class PromptInput:
    async def read_prompt(self, prompt: str) -> InputResult: ...
    async def read_approval(self, prompt: str) -> InputResult: ...
```

`PromptInput` 只负责终端编辑、EOF / Ctrl-C 翻译、TTY / stream 输入差异和 approval 输入隔离；它不决定哪些文本属于可持久化 prompt 历史。`InputResult.kind == "text"` 表示用户提交文本，`"cancelled"` 表示 Ctrl-C 取消本地编辑/approval UI，`"eof"` 表示 Ctrl-D 或 stream EOF。历史写入由 REPL loop 在完成语义分类后显式提交给 `PromptHistory`，避免把 slash command、approval decision、退出命令或未来控制命令的业务规则塞进输入 adapter。

具体实现：

- `PromptToolkitInput`：TTY 实现，内部使用 `PromptSession`。
- `StreamInput`：非 TTY/simple stream 实现，用新代码读取 `TextIO`，不包装 `_read_line(...)` 或 `_StdinReader`。

`run_interactive(...)`、`_reenter_actionable(...)`、`_resolve_approvals(...)` 和测试应该依赖这个 abstraction，而不是直接读取 `sys.stdin` 或调用 `_read_line(...)`。迁移完成后，旧 `_StdinReader` / `_read_line(...)` 应从代码中删除，而不是作为 fallback、兼容层或死代码保留。

### 接口边界

这个改造只替换 CLI 输入实现，不改变既有 runtime/control 接口：

- 保持 `AgentRuntime.start(...)`、`continue_run(...)`、`resume(...)`、`approve(...)`、`deny(...)` 的调用语义。
- 保持 `run_interactive(runtime, console)`、`run_single(...)`、`run_resume(...)` 的外部入口语义和函数签名。测试注入通过模块内 `_make_prompt_input(...)` / `_make_prompt_history(...)` factory seam 完成，不新增 `input_adapter=` 这类公共参数。
- 保持 active turn 的 `_drive_session_to_result(...)` interrupt driver。新输入层只负责 idle prompt / approval prompt；agent 正在运行时的 Ctrl-C 仍由 foreground `RunSession` interrupt 机制处理。
- 保持 `WAITING_APPROVAL`、`WAITING_TOOL_RESULT`、`INTERRUPTED`、`PAUSED` 的 durable status 语义不变。

### 依赖

在 `packages/knuth-cli/pyproject.toml` 增加普通运行时依赖 `prompt_toolkit>=3.0.50`，并更新 `uv.lock`。它不是 optional extra；升级后的 TTY REPL 依赖 prompt-toolkit，初始化失败时报错退出。

理由：

- `PromptSession` 面向重复 prompt 调用，并在调用之间保留 history。
- 自定义 `PromptHistory` 支持持久 prompt 历史，并可直接作为 prompt-toolkit history 使用。
- `Completer`、`Completion`、`ThreadedCompleter`、`complete_in_thread`、`complete_while_typing` 覆盖补全，不需要手写 raw terminal parser。
- `KeyBindings` 和 `EditingMode.EMACS` 覆盖必需按键语义。
- `prompt_async(...)` 让 AnyIO/async REPL 可以 await 输入，而不会重新引入旧 stdin worker thread。

### 历史存储

新增 `knuth_cli/input_history.py`，提供一个具体的 `PromptHistory`：

- 文件路径：`platformdirs.user_data_dir("knuth") / "knuth-cli" / "history.jsonl"`。
- 记录形状：

```json
{"text":"explain this file","project_key":"/abs/project-root","cwd":"/abs/project/subdir","session_id":"...","timestamp":"2026-06-21T10:00:00Z","kind":"prompt"}
```

- 写入使用 append-only JSONL。
- 读取按最新优先扫描。v1 可以带 cap 地把所有行读入内存；如果文件超过 cap，只读 tail。
- Cap：读取最新 1000 条；compaction 可作为后续工作。
- 默认只导航当前 `ProjectKey` 的历史。
- 存储层是 append-only 的 prompt 提交事件流，只折叠连续重复写入；非连续重复保留各自 timestamp / session metadata。导航和搜索层再按最新优先展示唯一文本，避免用户在 Up / `Ctrl-R` 中反复撞到相同 prompt。
- `ProjectKey` 解析规则：如果当前目录位于 git repo 内，使用 `realpath(git_root)`；否则使用 `realpath(cwd)`。解析失败时 fallback 到 `realpath(cwd)`。记录中仍保留提交时的 `cwd`，用于审计。
- 当前 session 内刚提交、尚未 flush 到文件的条目也应进入 in-session memory。
- `PromptHistory` 可以直接接给 prompt-toolkit 作为编辑器历史源，同时暴露 `append_prompt(...)` 给 REPL loop。不要为了未来替换编辑器再拆一个额外 adapter。
- 只有 REPL loop 能调用 `append_prompt(...)` 写入普通用户 prompt。
- Prompt-toolkit 不拥有历史写入权。主 prompt 调用必须禁用自动 history append（例如 `add_to_history=False` 或等价配置），避免 slash command、退出命令或其它控制输入绕过 REPL 语义分类进入历史。
- `history.append_prompt(...)` 成功后应更新进程内历史视图，让下一次 Up / `Ctrl-R` 能立即找到刚提交的 prompt。

这保留 project/session-aware history 的产品语义，但不引入 paste-store 复杂度。

### Prompt Session 配置

每个交互式 `knuth run` 进程创建一个 `PromptSession`：

- `editing_mode=EditingMode.EMACS`
- `history=PromptHistory(...)`
- `add_to_history=False` 或等价方式禁用 prompt-toolkit 自动写入；历史写入只发生在 REPL loop 的 `history.append_prompt(...)`
- `auto_suggest=AutoSuggestFromHistory()`
- `completer=ThreadedCompleter(KnuthCompleter(...))` 或 `complete_in_thread=True`
- `complete_while_typing=False`，v1 避免和 Up/Down 历史搜索冲突
- history search 配置优先使用 prompt-toolkit 内置能力；只有内置 `Ctrl-R` 语义无法满足验收时，才切换到 Knuth 自定义 controller
- `multiline=True`
- `prompt_continuation` 渲染紧凑 continuation margin，例如 `"      ... "`
- `wrap_lines=True`
- `mouse_support=False`
- `key_bindings=build_knuth_key_bindings(...)`
- `bottom_toolbar` 最多展示很轻量的临时模式文本，例如 reverse-search query；v1 不做大 footer

因为 prompt-toolkit 的默认 multiline mode 使用 `Esc`+`Enter` / `Meta`+`Enter` 接受输入，Knuth 应该绑定普通 Enter 为提交，显式 newline gesture 为插入换行。如果 prompt-toolkit 内置 multiline accept 行为冲突，则把 multiline 实现为单 buffer 加自定义 key binding，在 Enter 时调用 `event.app.exit(result=buffer.text)`。

产品语义上，`knuth run` 仍是以快速提交为主的 REPL，而不是全屏文档编辑器。普通 Enter 应保持“提交当前 prompt”的肌肉记忆；多行能力是增强项，通过显式 newline gesture 进入。粘贴多行文本仍应原样保留换行，避免用户为了输入长 prompt 被迫切换到外部编辑器。

### Keybinding 设计

采用小型 action vocabulary：

- Global：`app:interrupt`、`app:exit`、`app:redraw`、`history:search`
- Chat：`chat:submit`、`chat:newline`、`history:previous`、`history:next`
- Autocomplete：accept / dismiss / previous / next
- HistorySearch：next / accept / cancel / execute

v1 可以把这些写成代码常量，不做用户可配置 JSON。关键是上下文 ownership：

- completion menu 打开时，Up/Down 导航 completion。
- reverse history search 激活时，按键更新 search state。
- 其他时候，由编辑 buffer 和历史导航拥有按键。

### 补全设计

新增 `knuth_cli/completion.py`：

- `SlashCommandCompleter`：第一个 token 的 slash command 补全。
- `RunIdCompleter`：来自 `runtime.runs(limit=20)` 的有界 snapshot。
- `ToolCompleter`：来自 `runtime.tools()` 的有界 snapshot。
- `KnuthCompleter`：根据 `Document.text_before_cursor` 路由。

候选获取策略：

- Slash command 静态补全必须立即可用，不依赖 runtime snapshot。
- REPL 启动时，以及每个可能改变 run 状态的命令后，后台刷新 run/tool snapshot；刷新不能阻塞 prompt 出现，也不能阻塞下一次 prompt。
- 刷新失败、超时或 snapshot 过期时，静态 slash command 补全仍可工作；run/tool 补全可以暂时缺失或使用旧 snapshot。
- `get_completions(...)` 只读取内存 snapshot，绝不 await runtime，也不同步调用 `runtime.runs()` 或 `runtime.tools()`。
- 命令执行路径仍由 runtime 校验真实 run id / tool name；补全只是 best-effort convenience。

### 反向历史搜索

v1 默认采用 prompt-toolkit 内置 reverse incremental search，不先自研搜索模式。只有验证发现内置能力无法满足以下硬验收时，才实现小型 `HistorySearchController`：

- `Ctrl-C` 取消搜索并恢复进入搜索前的 draft。
- `Esc` / `Tab` 接受匹配但不提交。
- `Enter` 接受并提交匹配项。

自定义 fallback 的行为要求：

- 进入搜索前捕获原 buffer text/cursor。
- 按最新优先扫描 `PromptHistory`。
- 维护 seen text set 以去重。
- `Ctrl-R` 前进到更旧的匹配项。
- `Esc`/`Tab` 接受到 buffer。
- `Enter` 用匹配结果退出 prompt。
- `Ctrl-C` 恢复原 buffer 并退出搜索模式。

### 集成点

`run_interactive(...)` 改为：

1. 构造 `history = _make_prompt_history(...)` 和 `input = _make_prompt_input(runtime, console, history)`。
2. 像今天一样 reenter actionable run。
3. `result = await input.read_prompt(_PROMPT)`。
4. 如果 `result.kind == "cancelled"`，丢弃 draft 并继续显示 prompt；如果 `result.kind == "eof"`，退出 REPL。
5. 对 `result.kind == "text"` 的输入，像今天一样路由 slash command 和 turn。
6. 只有当 REPL 判定输入是 TTY 普通用户 prompt，并准备调用 `_run_turn(...)` 时，才先调用 `history.append_prompt(prompt, ...)` 记录历史，再启动 `_run_turn(...)`。
7. 历史记录的是“用户提交过什么”，不依赖 agent turn 成功；即使 `_run_turn(...)` 后续失败，刚提交的 prompt 也应能通过历史找回。

`_resolve_approvals(...)` 和 `_reenter_actionable(...)` 都接收同一个 input adapter；approval 输入使用 `input.read_approval(...)`。`read_approval(...)` 可以和主 prompt 共享 adapter 对象，但不能共享主 prompt 的 history、completion、multiline、autosuggest 或 history-search 状态。

`run_single(...)` 不使用新 prompt session。

测试需要 fake input 时，patch 模块内 factory 返回 fake adapter；`PromptToolkitInput`、`StreamInput` 和 `PromptHistory` 的具体行为通过 lower-level 单元测试直接覆盖。

### 测试策略

单元测试：

- 历史写入/读取/去重/ProjectKey 过滤。
- Slash command completion candidate 和 run/tool metadata。
- 如果可行，用 prompt-toolkit test input/output utilities 测试 multiline submit/newline key 行为。
- 非 TTY/simple stream input 选择。
- Approval prompt 不记录历史。
- 现有 mock `_read_line(...)` 测试迁移到 fake input adapter 或 fake stream。

默认 PTY 回归：

- 在真实 PTY 中启动 `uv run knuth run`，确认 prompt 出现且不挂死。
- prompt-level Ctrl-C 后立刻输入 `/exit`，确认之前的 late-line bug 不复现。
- 非 TTY pipe 输入 `/exit` 仍可用。

Opt-in PTY smoke：

- 输入 prompt，确认写入历史；重启 REPL，按 Up，确认 prompt 出现。
- 用显式 newline gesture 输入多行 prompt，提交后确认 runtime 收到换行。
- 输入 `Ctrl-A/E/K/U/W`，在可观测范围内确认 buffer 行为。
- 输入 `/res<Tab>`，确认补全为 `/resume`。
- 按 `Ctrl-R`，输入子串，接受，确认文本恢复。
- 到达 approval prompt，按 Ctrl-C，确认 run 保持 `WAITING_APPROVAL`。

验证命令：

- `uv run python -m unittest discover -s tests -v`
- `uv run python -m compileall packages tests`
- `git diff --check`
- 默认 PTY 回归进入 `uv run python -m unittest discover -s tests -v`；如果当前系统没有可用 PTY 或终端条件不满足，测试用 `unittest.skip` 跳过。
- Prompt history、multiline、completion、Ctrl-R 和 approval Ctrl-C 作为 opt-in PTY smoke，通过 `KNUTH_PTY_SMOKE=1` 或等价开关启用。

## 实现计划

### Phase 1: 输入抽象和旧机制替换

- 新增 `knuth_cli.input`，包含 `PromptInput`、`PromptToolkitInput`、`StreamInput` 和 factory detection。
- 把顶层 prompt 读取移到 adapter 后面。
- 把 approval prompt 和 reentry approval 流程也移到同一个 adapter 后面。
- 迁移相关测试后，删除 `_read_line(...)` / `_StdinReader` 实现。
- 用 `rg "_read_line|_StdinReader"` 验证 REPL 路径、测试和新输入层没有残留引用。
- 在加入复杂 prompt-toolkit 行为前，先确认新 simple stream 路径保持外部 CLI 行为。

### Phase 2: 持久历史

- 新增 `input_history.py`，包含 append/read/filter/dedupe。
- 只在普通用户 prompt 提交后记录历史，不记录 slash command、approval answer 或 `/exit`。
- 把 `PromptToolkitInput` 接到 `PromptHistory`。
- 增加历史语义单元测试。

### Phase 3: Prompt Toolkit TTY Session

- 增加依赖并更新 lock。
- 创建包含 Emacs editing、multiline、wrapping、autosuggest 和 continuation prompt 的 `PromptSession`；TTY 初始化失败时报错退出。
- 对 defaults 不足的 Enter/newline/Ctrl-D/Ctrl-C/Ctrl-L 增加小型 keybinding 层。
- 保留 Ctrl-C 语义：prompt-level `KeyboardInterrupt` 转为“取消本地编辑、丢弃 draft、回到干净 prompt”，绝不变成 process exit。

### Phase 4: 补全

- 实现静态 slash command 补全。
- 增加 snapshot-backed run/tool completion。
- 确保补全非阻塞，并在改变 run 状态的命令后刷新 snapshot。
- 增加 completion routing 测试。

### Phase 5: 反向历史搜索

- 先验证 prompt-toolkit 内置 reverse incremental search。
- 只有当内置能力无法满足 `Ctrl-C` 恢复 draft、`Esc` / `Tab` 接受但不提交、`Enter` 接受并提交这三条硬验收时，才增加小型自定义 history-search controller。
- 增加 draft preservation、execute/accept/cancel 行为的 PTY 验收。

### Phase 6: PTY 回归套件

- 新增或扩展 repo-local PTY helper，用于交互测试。
- 默认覆盖已知失败族：prompt 下 Ctrl-C 后立刻输入下一条命令，以及非 TTY pipe `/exit`。
- 新输入能力细节和 approval Ctrl-C 放入 opt-in PTY smoke，避免 CI 终端差异导致主测试套件抖动。
- 默认 PTY 回归应被 `unittest discover` 收到；环境缺少 PTY 支持时 skip，而不是要求手动运行额外脚本。

## 风险与缓解

- 风险：prompt-toolkit 和 Rich 都写终端，可能产生视觉干扰。
  缓解：只在 idle input 阶段让 prompt-toolkit 拥有终端；runtime rendering 开始前结束 prompt；active input 外继续用 Rich。
- 风险：prompt-toolkit 默认 SIGINT handling 和 Knuth run interrupt 语义冲突。
  缓解：prompt session 只存在于 idle 阶段；active turn 仍走 `_drive_session_to_result(...)`。
- 风险：新输入层重写 stdin 后丢失旧 `_StdinReader` 曾经保护的 Ctrl-C late-line 语义。
  缓解：把 Ctrl-C 语义写成新输入层 contract，并用真实 PTY 覆盖 `Ctrl-C -> prompt returns -> immediate next command`。
- 风险：multiline Enter 语义和 prompt-toolkit defaults 不一致。
  缓解：用显式 key bindings 和 PTY 测试定义 Knuth 行为。
- 风险：历史文件无限增长。
  缓解：v1 读取加 cap；真实使用后再做 compaction issue。
- 风险：completion snapshot 过期。
  缓解：静态命令始终可用；run-changing command 后刷新 snapshot；过期 run-id completion 无害，runtime 会校验实际命令执行。

## 验收标准

- [x] TTY 中 Up/Down 可以导航持久 prompt 历史，并恢复 draft。
- [x] `Ctrl-R` 可以反向搜索当前 `ProjectKey` 的 prompt 历史。
- [x] 可以编辑并提交包含换行符的多行 prompt。
- [x] R3 列出的 Emacs/readline 编辑按键在主 prompt 中可用。
- [x] Slash command 和相关参数可以补全，且不阻塞 prompt。
- [x] Prompt-level Ctrl-C 留在 REPL；active-turn Ctrl-C 仍 interrupt `RunSession`。
- [x] Approval Ctrl-C 让 run 保持 `WAITING_APPROVAL`。
- [x] 非 TTY/script 路径保持现有外部行为，但不依赖旧 `_StdinReader`。
- [x] 验证命令和真实 PTY smoke 通过。
