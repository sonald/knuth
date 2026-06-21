# Prompt Toolkit 编辑器

Status: done

## 描述

接入基于 prompt-toolkit 的 TTY prompt，提供 Emacs/readline 编辑、多行输入、基于历史的 autosuggest 和视觉换行。TTY 路径不再使用旧 `_StdinReader`；prompt-toolkit 初始化失败时报错退出，不做降级输入模式。

## 验收标准

- [x] `packages/knuth-cli/pyproject.toml` 声明普通运行时依赖 `prompt_toolkit>=3.0.50`，不是 optional extra，并更新 `uv.lock`。
- [x] 主 TTY prompt 使用带 Emacs editing mode 的 `PromptSession`。
- [x] Enter 提交；显式 newline gesture 插入换行。
- [x] 必需 Emacs/readline 按键在主 prompt 中可用。
- [x] Prompt-level Ctrl-C 不退出 REPL。
- [x] TTY 下 prompt-toolkit 初始化失败时报错退出，不降级到 `StreamInput`，也不回退到旧 `_StdinReader`。

## 验证

- `uv lock`
- `uv run python -m unittest discover -s tests -v`
- 默认 PTY 回归覆盖 prompt-level Ctrl-C 后 immediate next command。
- Up 历史和多行输入放入 `KNUTH_PTY_SMOKE=1` opt-in 真实 PTY smoke。

## 文件

- `packages/knuth-cli/pyproject.toml`
- `uv.lock`
- `packages/knuth-cli/src/knuth_cli/input.py`
- `tests/test_cli.py`
