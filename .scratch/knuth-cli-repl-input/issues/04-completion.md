# Slash Command 和参数补全

Status: done

## 描述

使用有界 runtime snapshot 为 slash command、run id 和 tool name 增加非阻塞补全。

## 验收标准

- [x] 第一个 token 的 slash command 补全包含内置 REPL 命令。
- [x] `/resume` 和 `/status` 在可用时补全最近 run id。
- [x] `/tools` 在有价值时补全 tool name 或 subcommand。
- [x] 补全在 keypress 路径上不会被慢 runtime call 阻塞。
- [x] Prompt 出现不等待 run/tool snapshot 刷新；静态 slash command 补全立即可用。
- [x] `get_completions(...)` 只读内存 snapshot，不 await runtime，也不同步调用 `runtime.runs()` 或 `runtime.tools()`。
- [x] Completion overlay 只在激活时拥有 `Tab`、`Esc`、Up、Down。

## 验证

- `uv run python -m unittest tests.test_cli -v`
- 对 `/res<Tab>` 做 `KNUTH_PTY_SMOKE=1` opt-in 真实 PTY smoke。

## 文件

- `packages/knuth-cli/src/knuth_cli/completion.py`
- `packages/knuth-cli/src/knuth_cli/input.py`
- `tests/test_cli.py`
