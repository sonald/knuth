# CLI 登录流程

Status: proposed

## 描述

让 CLI 用户可以启动由 ChatGPT subscription 支持的 run，并在终端中完成 LiteLLM 的 device-code flow。

## 验收标准

- [ ] 在 `KNUTH_MODEL=chatgpt/...` 且没有 token file 时，CLI 能进入 LiteLLM 的 device-code prompt。
- [ ] ChatGPT auth mode 下，CLI 不要求 `KNUTH_API_KEY` 或 `KNUTH_BASE_URL`。
- [ ] 首次登录 timeout 被报告为 provider/auth error，而不是 Knuth configuration 缺失错误。
- [ ] Token files 不会被复制到 `.env`、YAML config、debug sinks、runtime events 或 ledger rows。
- [ ] Manual smoke script 说明如何用临时 `CHATGPT_TOKEN_DIR` 验证，且不触碰用户全局 LiteLLM auth 目录。
- [ ] CLI smoke 覆盖一次带工具定义的请求，确认 LiteLLM Chat Completions bridge 能产生 Knuth 可识别的 tool call events。
- [ ] CLI 不新增 `/login-chatgpt` 命令；登录由第一次模型请求触发。

## Comments

- 第一版本可以依赖 LiteLLM 打印 verification URL 和 code。更精致的 Knuth-native login command 可以等这条路径证明有用后再做。
