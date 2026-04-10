---
name: docs-agent
description: Validates docs/ knowledge base — checks frontmatter, MOC coverage, wikilink integrity, and duplicate aliases
trigger: /docs-validate
---

# Documentation Validator

Run `npx tsx .claude/skills/docs-agent/validate-docs.ts` to check the `docs/` knowledge base for:

1. **YAML frontmatter** — every `.md` must have `title`, `tags`, `status`
2. **MOC coverage** — every doc must appear in `docs/MOC.md`
3. **Wikilink integrity** — no broken `[[links]]`
4. **Duplicate aliases** — no two docs share the same alias

Fix any warnings before committing documentation changes.
