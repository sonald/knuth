# PRD：ChatGPT 订阅认证

## 状态

提议中

## 目标

让 Knuth 可以通过 LiteLLM 的 `chatgpt/` provider 调用 ChatGPT 订阅/Codex 权益模型，从而在没有 OpenAI Platform API Key 的情况下运行模型请求。

## 调研结论

- LiteLLM 文档提供了一等的 ChatGPT Subscription provider，模型名前缀是 `chatgpt/`。
- LiteLLM 说明该 provider 使用 OAuth device-code 认证，本地保存 token，并支持 `CHATGPT_TOKEN_DIR`、`CHATGPT_AUTH_FILE`、`CHATGPT_API_BASE`、`OPENAI_CHATGPT_API_BASE`、`CHATGPT_ORIGINATOR`、`CHATGPT_USER_AGENT*` 等环境变量。
- LiteLLM 推荐 Codex 模型走 Responses，但也会把支持的 `chatgpt/` 模型的 `/chat/completions` 请求桥接到 Responses。
- LiteLLM 会为该 provider 去掉 ChatGPT backend 拒收的 token limit 和 metadata 字段。
- 本仓库当前安装的 LiteLLM 包已经包含 `litellm/llms/chatgpt`。
- OpenAI Codex 文档区分 ChatGPT 登录/订阅访问和 API Key 用量计费访问。Codex access token 面向可信的 Codex local workflow，目前限定在 Business/Enterprise 范围，并没有被文档描述为通用 OpenAI API 凭据。

参考：

- https://docs.litellm.ai/docs/providers/chatgpt
- https://docs.litellm.ai/docs/providers/openai
- https://developers.openai.com/codex/auth
- https://developers.openai.com/codex/enterprise/access-tokens

## 当前 Knuth 状态

- `LiteLLMInferenceClient` 已经会保留带 provider 前缀的模型名，因为 `_litellm_model_name()` 对包含 `/` 的模型名原样返回。
- 因此 `chatgpt/gpt-5.3-codex` 可以不改 runtime loop，直接以 `chatgpt/` 模型名传给 LiteLLM。
- CLI config 当前强制要求 `api_key` 和 `base_url`，即使 provider 并不需要 Knuth 自己持有 API 凭据。
- IM sidecar 当前不走 `knuth_cli.config.load_config()`，而是在 `packages/knuth-im/src/knuth_im/runtime_factory.py` 中直接要求 `KNUTH_API_KEY`、`KNUTH_BASE_URL` 和 `KNUTH_MODEL`。
- IM desktop settings PRD 已经把本地模型设置和 API Key 存储归到 Electron host，因此 ChatGPT 认证也应该属于 host/config 层，不应该进入 `knuth-runtime`。

## 范围

- CLI 和 IM desktop 配置支持 `chatgpt/` 模型名，并且不要求 `KNUTH_API_KEY` 或 `KNUTH_BASE_URL`。
- 增加 host 拥有的 ChatGPT 认证模式，可以显式配置为 `auth_mode=chatgpt`，也可以从 `model.startswith("chatgpt/")` 推导。
- 第一版本让 LiteLLM 负责 OAuth device flow 和 token refresh。
- app 托管的流程通过设置 `CHATGPT_TOKEN_DIR` 提供 Knuth 拥有的 token 目录，避免 packaged IM 把不透明认证状态写入意外的全局目录。
- CLI 清楚展示 LiteLLM device-code 登录提示；IM 在 sidecar 需要首次登录时通过日志/状态暴露该信息。
- 原始 ChatGPT OAuth token 不进入 renderer 可见状态、runtime events、durable ledger rows 或 debug artifacts。
- 验证 Knuth 当前 `acompletion`/Chat Completions 路径经过 LiteLLM bridge 后，是否完整支持 streaming、tool calls、tool results 和 interrupt。

## 非目标

- 除非 LiteLLM 无法提供所需 UX，否则不实现 Knuth 自己的 OAuth 协议副本。
- 不抓取或依赖 Codex CLI 的私有本地 auth storage。
- 除非 LiteLLM 或 OpenAI 文档明确模型推理支持该契约，否则不把 `CODEX_ACCESS_TOKEN` 当作通用模型 API 凭据。
- 不把 provider credential 所有权移动到 `knuth-runtime`。
- 第一版本不增加多账号 provider profile；需要切换账号时，只提供清除 ChatGPT 登录状态。
- 第一版本不新增 CLI `/login-chatgpt` 命令。

## 设计

第一版本：解锁配置约束，并提供 host 拥有的 token 目录。

1. 扩展 config，加入 auth/provider mode：
   - 普通 OpenAI-compatible/API-key 模式仍然要求 `api_key` 和 `base_url`。
   - 对 `chatgpt/` 模型，`api_key` 和 `base_url` 可选；ChatGPT mode 下不把 `KNUTH_API_KEY`/`KNUTH_BASE_URL` 作为模型认证参数传入 LiteLLM。
   - 认证模式优先级为：显式 `auth_mode` > `model` provider 前缀推导 > API-key mode 默认值。
   - ChatGPT mode 下完全忽略 `KNUTH_API_KEY`/`KNUTH_BASE_URL`；ChatGPT backend override 使用 LiteLLM 定义的 `CHATGPT_API_BASE`/`OPENAI_CHATGPT_API_BASE`，不要复用 `KNUTH_BASE_URL`。
   - 如有需要，保留 `provider_options` 或窄 env mapping 来传递 LiteLLM provider-specific 值。

2. CLI 行为：
   - 用户可以设置 `KNUTH_MODEL=chatgpt/gpt-5.3-codex`，并在没有 `KNUTH_API_KEY` 的情况下运行 Knuth。
   - 如果没有 LiteLLM token，LiteLLM 打印 device-code 登录流程，并阻塞到授权完成或超时。
   - 可选的 `KNUTH_CHATGPT_TOKEN_DIR` 映射到 `CHATGPT_TOKEN_DIR`；否则 CLI 允许使用 LiteLLM 默认目录。
   - CLI 第一版不增加专门登录命令，登录由第一次模型请求触发。

3. IM desktop 行为：
   - settings 增加认证方式：API Key 与 ChatGPT subscription。
   - ChatGPT mode 隐藏 API-key 输入，只要求 model、timeout、workspace、database path。
   - 第一版接受 LiteLLM 明文 `auth.json` token 文件；Main process 创建 `0700` token 目录，并在启动 sidecar 时传入 `CHATGPT_TOKEN_DIR=<app userData>/litellm-chatgpt`。如果需要补权限，使用 `0600` 修正 auth 文件，不引入 Keychain。
   - 第一版单账号，不做 profile selector；IM 只提供清除 ChatGPT 登录状态来支持换号或恢复坏 token。清除只删除本地 LiteLLM token 文件/目录，不做 OAuth revoke。
   - Renderer 只看到 `authMode`、`needsLogin`、provider/model、device-code URL/code 等公开认证状态，永远看不到 token 内容。
   - 由于 LiteLLM 登录通常发生在第一次模型请求而不是 `/healthz`，IM 需要显式 auth preflight 或 first-run login 状态桥接；不能只依赖 backend startup health 来判断是否需要登录。
   - Auth preflight 只在用户主动点“登录/验证”或首次发送消息时触发，执行一个极短模型请求；不在后台自动触发。
   - Sidecar stdout/stderr 中的 LiteLLM device-code 提示必须被 main process 捕获并转换成 renderer 可见状态，否则 packaged app 用户无法完成登录。

4. 可选的后续 Codex access-token bridge：
   - 如果 OpenAI/LiteLLM 文档明确支持直接注入 token，再增加独立的 `codex_access_token` auth mode。
   - 在此之前，稳定方案是用 LiteLLM 的 ChatGPT provider device flow 获得订阅访问；Codex access token 只限 Codex CLI/automation workflow。

5. LiteLLM bridge 验证门槛：
   - Knuth 当前模型边界使用 `litellm.acompletion()`，而 LiteLLM 文档推荐 Codex 模型使用 Responses。
   - 第一版本可以先依赖 LiteLLM 的 Chat Completions -> Responses bridge，但必须用真实或协议级 smoke 验证 streaming content、reasoning、tool call delta/completed、tool result continuation 都能闭环。
   - 如果 bridge 不能满足 Knuth 的工具调用契约，则需要新增 Responses API inference adapter，而不是在 runtime 层绕过。

## 验收标准

- [ ] `KNUTH_MODEL=chatgpt/gpt-5.3-codex` 在缺少 `KNUTH_API_KEY` 或 `KNUTH_BASE_URL` 时不会 config loading 失败。
- [ ] CLI config 和 IM sidecar runtime factory 都支持 ChatGPT auth mode。
- [ ] `LiteLLMInferenceClient` 在未设置 `api_key` 和 `base_url` 时不传递这两个字段。
- [ ] focused unit test 证明 provider-prefixed `chatgpt/...` 原样传递，不会添加 OpenAI 前缀。
- [ ] CLI smoke path 在没有 token 时能进入 LiteLLM 的 ChatGPT auth code，并且不会把 secrets 写入 Knuth ledger/debug output。
- [ ] IM desktop settings 可以切换到 ChatGPT subscription mode，并用 app user data 下的 `CHATGPT_TOKEN_DIR` 重启 sidecar。
- [ ] IM desktop 提供清除 ChatGPT 登录状态入口，删除本地 LiteLLM token 后回到需要登录状态。
- [ ] IM desktop 能把 device-code 登录 URL/code 以 renderer 可见、token-free 的方式呈现给用户。
- [ ] ChatGPT bridge path 通过 streaming + tool calls 的端到端验证；否则记录并切换到 Responses adapter 方案。
- [ ] Renderer-visible state 永远不包含原始 OAuth access token 或 refresh token。

## 验证命令

- `uv run python -m unittest tests.test_cli_config tests.test_llmd`
- `uv run python -m unittest tests.test_knuth_im tests.test_agui_spike`
- `uv run python scripts/llmd_event_probe.py --model chatgpt/gpt-5.3-codex --prompt "Say hello in one short sentence."`
- `cd apps/knuth-im-web && npm run smoke:settings`
- `cd apps/knuth-im-web && npm run typecheck`
- `cd apps/knuth-im-web && npm run build`
- `cd apps/knuth-im-web && npm run smoke:sidecar-binary`
