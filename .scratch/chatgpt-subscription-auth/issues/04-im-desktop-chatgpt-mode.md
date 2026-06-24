# IM Desktop ChatGPT 模式

Status: proposed

## 描述

在 packaged Knuth IM settings 和 sidecar launch path 中增加 ChatGPT subscription mode。

## 验收标准

- [ ] Settings UI 提供 API-key mode 和 ChatGPT subscription mode。
- [ ] ChatGPT mode 隐藏原始 API-key 字段，并保持 renderer-visible state 不含 token。
- [ ] Main process 只把公开的 auth mode/model settings 存入 public settings file。
- [ ] Main process 创建 `0700` token 目录，并用 Electron app user data 下的 `CHATGPT_TOKEN_DIR` 启动 sidecar；如需修正 LiteLLM `auth.json`，文件权限为 `0600`。
- [ ] ChatGPT mode 不支持多账号 profile；settings 只提供清除 ChatGPT 登录状态。
- [ ] 清除 ChatGPT 登录状态只删除本地 LiteLLM token 文件/目录，不做 OAuth revoke，完成后 sidecar 回到 login required。
- [ ] Sidecar startup status 能区分缺少 model settings、ChatGPT login required、provider auth timeout 和普通 backend crash。
- [ ] Main process 捕获 LiteLLM device-code 登录提示，并把 URL/code 转成 renderer 可见、token-free 的登录状态。
- [ ] 由于 `/healthz` 不触发模型认证，IM 增加 auth preflight 或 first-run login bridge，不能只依赖 sidecar 启动成功判断 ChatGPT 认证可用。
- [ ] Auth preflight 只在用户主动点“登录/验证”或首次发送消息时触发，执行极短模型请求，不在后台自动触发。
- [ ] `npm run smoke:settings`、`npm run typecheck`、`npm run build` 和 `npm run smoke:sidecar-binary` 覆盖 settings 与 packaging path。

## Comments

- 复用 `.scratch/knuth-im-desktop-settings/` 里已有的 desktop settings ownership boundary。
