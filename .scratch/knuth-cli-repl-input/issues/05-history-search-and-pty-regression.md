# 历史搜索与 PTY 回归

Status: done

## 描述

先验证 prompt-toolkit 内置反向历史搜索能否满足 Knuth 的硬验收；只有内置能力失败时，才实现自定义 history-search controller。同时为升级后的 REPL 输入面增加真实 PTY 覆盖。

实现备注：真实 PTY 验证发现内置搜索在“带 draft 时搜索并用 Esc 接受但不提交”的语义上不满足验收，因此本次加入了 CLI-local 的小型 history-search state。

## 验收标准

- [x] `Ctrl-R` 启动反向搜索。
- [x] 重复按 `Ctrl-R` 前进到更旧的匹配项。
- [x] `Enter` 提交选中的匹配项。
- [x] `Esc` 或 `Tab` 接受但不提交。
- [x] `Ctrl-C` 取消搜索并恢复原 draft。
- [x] 默认实现优先使用 prompt-toolkit 内置搜索；只有上述 accept/execute/cancel 语义无法满足时，才加入自定义 controller。
- [x] 默认 PTY 回归覆盖启动不挂死、prompt Ctrl-C 后立即输入 `/exit`、非 TTY pipe `/exit`。
- [x] 默认 PTY 回归进入 `unittest discover`；环境缺少 PTY 支持时用 `unittest.skip` 跳过。
- [x] Opt-in PTY smoke 覆盖 history、multiline、completion、`Ctrl-R` 和 approval Ctrl-C。

## 验证

- `uv run python -m unittest discover -s tests -v`
- `uv run python -m compileall packages tests`
- `git diff --check`
- 默认真实 PTY 回归随常规测试运行，缺少 PTY 条件时 skip。
- 对 history、multiline、completion、`Ctrl-R` 和 approval Ctrl-C 做 `KNUTH_PTY_SMOKE=1` opt-in 真实 PTY smoke。

## 文件

- `packages/knuth-cli/src/knuth_cli/input.py`
- `packages/knuth-cli/src/knuth_cli/input_history.py`
- `tests/test_cli.py`
- `tests/test_cli_interrupt.py`
- 可选：`tests/test_cli_repl_pty.py`
