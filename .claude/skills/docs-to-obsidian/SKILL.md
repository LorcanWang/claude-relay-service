---
name: docs-to-obsidian
description: Compiles docs/ into an Obsidian-compatible vault with frontmatter, wikilinks, and a Map of Content
trigger: /docs-compile
---

# Obsidian Vault Compiler

Run `npx tsx .claude/skills/docs-to-obsidian/compile-vault.ts` to:

1. Create `.obsidian/` config in `docs/`
2. Add YAML frontmatter (title, aliases, tags, dates, status) to any doc missing it
3. Auto-insert `[[wikilinks]]` between related docs
4. Generate `docs/MOC.md` — the Map of Content index grouped by category

After compiling, open `docs/` as an Obsidian vault to browse the knowledge graph.
