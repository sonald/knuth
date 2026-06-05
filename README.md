# Knuth Agent Framework

Knuth is split into independently importable component packages plus a reference
CLI. The component packages are intended to remain usable both in-process today
and behind service clients later.

## Packages

- `knuth-core` / `knuth.core`: shared message, event, run, and status models.
- `knuth-llmd` / `knuth_llmd`: LLM client protocol and LiteLLM adapter.
- `knuth-toold` / `knuth_toold`: tool contracts, registry, and execution.
- `knuth-runtime` / `knuth_runtime`: orchestration layer that composes LLM and
  tools.
- `knuth-cli` / `knuth_cli`: reference command-line interface.

`knuth-runtime` depends on the component packages and assembles them. The lower
level components should not depend on the runtime or CLI.
