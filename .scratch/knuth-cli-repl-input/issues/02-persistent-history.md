# 持久 Prompt 历史

Status: done

## 描述

为已提交的 TTY 普通用户 REPL prompt 增加 append-only 历史，按 `ProjectKey` 和 session metadata 记录，并保留提交时的 cwd。Slash command、approval answer、退出命令和非 TTY / pipe 输入不能写入历史。

## 验收标准

- [x] 已提交的非空 TTY 普通用户 prompt 写入 CLI-owned history file。
- [x] 历史写入发生在 `_run_turn(...)` 启动前；即使 turn 失败，刚提交的 prompt 也能通过历史找回。
- [x] Prompt-toolkit 自动 history append 被禁用；REPL loop 是唯一调用 `history.append_prompt(...)` 的写入者。
- [x] 写入后进程内历史视图立即可见，下一次 Up / `Ctrl-R` 能找到刚提交的 prompt。
- [x] Slash command、approval answer、`/exit`、`/quit` 不写入历史。
- [x] 非 TTY / pipe 输入不写入历史。
- [x] 历史导航/搜索按最新优先读取当前 `ProjectKey` 的条目。
- [x] 同一 git project 的不同子目录共享 prompt 历史；非 git 目录 fallback 到当前 cwd 的 realpath。
- [x] `/new` / `/clear` 后新 prompt 使用新的 history `session_id`，但同一 `ProjectKey` 的旧 prompt 仍可导航/搜索。
- [x] 连续重复写入会被折叠。
- [x] 非连续重复保留为不同提交事件；导航/搜索按最新优先展示唯一文本。
- [x] 用户向下导航越过最新历史项时，恢复 draft input。
- [x] 历史读写失败时优雅降级。

## 验证

- `uv run python -m unittest tests.test_cli -v`
- 为 `input_history.py` 增加 focused unit tests。

## 文件

- `packages/knuth-cli/src/knuth_cli/input_history.py`
- `packages/knuth-cli/src/knuth_cli/input.py`
- `tests/test_cli.py`
