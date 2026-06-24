# LiteLLM ChatGPT Provider

Status: proposed

## 描述

当模型使用 `chatgpt/` provider 且 Knuth 没有 API Key 或 base URL 可传递时，让 LiteLLM inference boundary 仍然能干净工作。

## 验收标准

- [ ] `_litellm_model_name("chatgpt/gpt-5.3-codex")` 返回原始模型名。
- [ ] 这些字段未设置时，`_base_kwargs()` 不包含 `api_key` 或 `base_url`。
- [ ] Provider options 可以透传，同时不向 runtime models 添加 provider-specific 概念。
- [ ] ChatGPT mode 下如果 `config.max_output_tokens` 被设置，验证 LiteLLM 不会向 ChatGPT backend 发送被拒收的 token limit 字段，或在 Knuth 侧按 provider mode 跳过该字段。
- [ ] Tests 证明普通无前缀模型仍然会变成 `openai/<model>`。
- [ ] Tests 证明显式 provider-prefixed models，包括 `chatgpt/...`，不会被重写。

## Comments

- 已安装的 LiteLLM 已经包含 `litellm/llms/chatgpt`，因此除非后续验证发现 locked version 有 bug，这个 issue 不需要依赖升级。
