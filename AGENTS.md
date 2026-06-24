# AGENTS

- 这是一个 uv 工程，所有命令都用 `uv` 运行。
- 所有文档用中文编写。

## Agent 技能

### Issue tracker

Issues 和 PRD 都以本地 Markdown 文件形式记录在 `.scratch/<feature-slug>/` 下。参见 `docs/agents/issue-tracker.md`。

### Triage labels

Triage 使用默认的五角色标签词汇。参见 `docs/agents/triage-labels.md`。

### 领域文档

这是一个 single-context repo：如果根目录存在 `CONTEXT.md`，需要读取它；同时读取 `docs/decisions/` 下的架构决策。参见 `docs/agents/domain.md`。

## IM desktop app 构建

Electron/Next.js app 位于 `apps/knuth-im-web`。

推荐的本地目录构建流程：

```sh
cd apps/knuth-im-web
npm run sidecar:build
npm run typecheck
npm run build
ELECTRON_MIRROR=${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/} npx electron-builder --mac --dir --publish never
npm run smoke:sidecar-binary
```

输出 app：

```text
apps/knuth-im-web/dist-electron/mac-arm64/Knuth IM.app
```

如果 `npm run typecheck` 因 `.next` 下过期的 Next 生成类型失败（例如仍引用已删除的 routes），删除 `.next` 后重新运行 `npm run typecheck` 和 `npm run build`。
