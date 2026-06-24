# Responses Bridge 工具调用验证

Status: proposed

## 描述

验证 LiteLLM `chatgpt/` provider 的 Chat Completions -> Responses bridge 是否满足 Knuth 当前模型边界的 streaming 和工具调用契约。如果不满足，第一版本需要改为新增 Responses API inference adapter。

## 验收标准

- [ ] 协议级测试覆盖 `stream=True` 下的 content delta、reasoning delta、tool call started/delta/completed 和 final assistant message。
- [ ] 端到端 smoke 覆盖模型发起工具调用、Knuth 执行/审批工具、tool result 继续喂回模型。
- [ ] 验证 `parallel_tool_calls=False`、`tools`、`tool_choice=auto` 在 `chatgpt/` provider 下被 LiteLLM 接受或被正确转换。
- [ ] 如果 bridge 缺少必要能力，记录明确失败点，并把实现方案改为 Responses API adapter，而不是在 runtime 层特殊处理。

## Comments

- LiteLLM 文档推荐 Codex 模型使用 Responses；当前 Knuth 使用 `litellm.acompletion()`，因此这是上线前必须确认的兼容性门槛。
