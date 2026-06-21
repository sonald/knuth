# 输入抽象与旧机制替换

Status: done

## 描述

引入 CLI-owned 输入 adapter，让新输入层完全接管顶层 prompt、approval prompt 和非 TTY stream 读取。旧 `_StdinReader` / `_read_line(...)` 不作为兼容目标；需要保留的是 Ctrl-C 的中断语义和 CLI 外部行为。

## 验收标准

- [x] `run_interactive(...)` 通过输入 adapter 读取 prompt。
- [x] `run_interactive(runtime, console)` 签名不变；测试通过 patch 模块内 input/history factory seam 注入 fake adapter。
- [x] 输入 adapter 返回 typed `InputResult`，明确区分 `text`、`cancelled` 和 `eof`，不使用 `None` 同时表示 Ctrl-C 和 EOF。
- [x] `_resolve_approvals(...)` 通过 adapter 读取 approval answer；approval prompt 使用独立最小配置，无历史、补全、autosuggest、多行或历史搜索。
- [x] `_reenter_actionable(...)` 复用同一个 adapter 处理 reentry approval。
- [x] 非 TTY 输入由新 `StreamInput` 实现，保持现有外部行为但不依赖 `_StdinReader`。
- [x] 旧 `_read_line(...)` / `_StdinReader` 实现已删除，不作为 fallback、兼容层或死代码保留。
- [x] Prompt-level Ctrl-C 留在 REPL；approval Ctrl-C 让 run 保持 `WAITING_APPROVAL`。
- [x] 不修改 runtime、ledger、event、AG-UI 代码或 CLI 子命令参数。

## 验证

- `uv run python -m unittest tests.test_cli tests.test_cli_interrupt -v`
- `uv run python -m compileall packages/knuth-cli/src tests`
- `rg "_read_line|_StdinReader" packages/knuth-cli/src tests`

## 文件

- `packages/knuth-cli/src/knuth_cli/repl.py`
- `packages/knuth-cli/src/knuth_cli/input.py`
- `tests/test_cli.py`
- `tests/test_cli_interrupt.py`
