# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Layout

This is a single-context repo.

- Read `CONTEXT.md` at the repo root when it exists.
- Read architectural decision records under `docs/decisions/` when they touch the area being changed.
- If these files do not exist, proceed silently. Do not suggest creating them upfront.

## Use the glossary's vocabulary

When output names a domain concept, use the term as defined in `CONTEXT.md`. If the concept is missing, note the gap instead of inventing new project language.

## Flag ADR conflicts

If output contradicts an existing decision document, surface it explicitly rather than silently overriding it.
