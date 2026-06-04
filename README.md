# Knuth Agent Framework

Knuth is split into independently importable component packages plus a reference
CLI. The component packages are intended to remain usable both in-process today
and behind service clients later.

## Packages

- `knuth-llmd` / `knuth_llmd`: LLM message types and client protocol.
- `knuth-toold` / `knuth_toold`: tool contracts, registry, and execution.
- `knuth-agentfsd` / `knuth_agentfsd`: agent-facing filesystem protocol.
- `knuth-runtime` / `knuth_runtime`: orchestration layer that composes LLM,
  tools, and filesystem access.
- `knuth-cli` / `knuth_cli`: reference command-line interface.

`knuth-runtime` depends on the component packages and assembles them. The lower
level components should not depend on the runtime or CLI.
