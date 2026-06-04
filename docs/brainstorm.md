# Knuth Agent Framework

## Brainstorm

### Core Concepts

Knuth 是一个重新设计的新的通用 Agent 运行时以及一个参考 cli。

### Package Boundaries

Knuth 的基础能力以独立组件包发布，并保持独立可 import：

- `knuth-llmd` (`knuth_llmd`): llm 的驱动层。目前考虑先用 litellm 封装。
- `knuth-toold` (`knuth_toold`): tool的管理 和 执行。
- `knuth-agentfsd` (`knuth_agentfsd`): agent 面向的 filesystem protocol。 这个现在用不上，还没想好。
- `knuth-runtime` (`knuth_runtime`): 组合 LLM、tool、filesystem 的运行时,  可复用、追踪、审计、中断恢复等的agent loop。
- `knuth-cli` (`knuth_cli`): 参考 CLI，用于测试基础能力，只依赖 runtime。

未来这些组件可以从 in-process 实现演进为独立 service client；runtime 和 CLI
只依赖稳定 protocol，不直接绑定具体部署形态。
