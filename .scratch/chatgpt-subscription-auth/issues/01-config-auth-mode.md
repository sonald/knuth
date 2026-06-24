# 配置认证模式

Status: proposed

## 描述

让 Knuth host configuration 可以表达不需要 Knuth 自己持有 API Key 凭据的 provider。第一类目标是 LiteLLM `chatgpt/` 模型。

## 验收标准

- [ ] ChatGPT subscription mode 下，`AgentConfig.api_key` 和 `AgentConfig.base_url` 可以缺省。
- [ ] 现有 API-key configuration 继续要求 model、API Key 和 base URL。
- [ ] `KNUTH_MODEL=chatgpt/...` 会推导出 ChatGPT auth mode，除非显式 config field 覆盖。
- [ ] 接受 `KNUTH_CHATGPT_TOKEN_DIR`，并映射到 child/model environment 的 `CHATGPT_TOKEN_DIR`。
- [ ] ChatGPT auth mode 下，`KNUTH_API_KEY`/`KNUTH_BASE_URL` 不会被当作模型认证参数传给 LiteLLM；ChatGPT backend override 只通过 `CHATGPT_API_BASE`/`OPENAI_CHATGPT_API_BASE` 表达。
- [ ] `packages/knuth-im/src/knuth_im/runtime_factory.py` 与 CLI config 使用同一套认证模式判断，避免 CLI 可用但 IM sidecar 仍要求 API Key。
- [ ] Config tests 覆盖 API-key mode、derived ChatGPT mode、explicit ChatGPT mode 和 missing required fields。

## Comments

- 这应该保持为 host/config 关注点。`knuth-runtime` 不应该知道 OAuth、ChatGPT 或 Codex auth。
