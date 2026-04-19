---
title: "Skill Manifest Evolution"
aliases: ["skill-manifest", "agent-json", "actions-array", "whenToUse"]
tags: [architecture, skills, orchestrator, agent-json, manifest, validation]
created: 2026-04-19
updated: 2026-04-19
status: active
---

# Skill Manifest Evolution

How `agent.json` files evolved from passive metadata into runtime contracts that the orchestrator enforces. Adopts patterns from the [Claude Code reference architecture](https://github.com/andrew-kramer-inno/claude-code-source-build) — specifically `loadSkillsDir.ts` — without copying its per-skill-tool fan-out (which would cost 50–140k input tokens per turn at our skill count).

## Motivation

Before this work, `agent.json` was display-only. The orchestrator dumped each enabled skill's full `SKILL.md` into the system prompt every turn, then handed the model a single generic `run_command(skill, command)` tool. Two problems:

1. **No safety affordances.** `update_budget` and `list_campaigns` were indistinguishable to the orchestrator. Couldn't show confirmation cards, mark destructive actions, or know which calls cost ad-spend.
2. **Bloated context.** Inline SKILL.md per skill ≈ 15–25KB of system prompt every turn, even when the user just said "hi".

See [[lynx-quality-architecture]] for the broader prompt-cost discussion and 10-round Codex review that settled this direction.

## Three frontmatter fields adopted from Claude Code

| Field | What it does | Visibility |
|---|---|---|
| `whenToUse` | One-line trigger guidance rendered as `_when: …_` under each entry in the skill index | Visible to model |
| `disableModelInvocation` | Filters the skill from `build_skill_index`, blocks `describe_skill`, refuses `run_command` | Hidden from model |
| `actions[]` | Declared subcommand list with safety metadata (see below) | Used by orchestrator validation |

These are read by `orchestrator/skill_loader.py` and surfaced to `orchestrator/main.py` at chat time.

## `actions[]` schema

Each action declares a discoverable verb of the skill plus structured safety flags:

```json
{
  "id": "update_budget",
  "title": "Change a campaign's daily budget",
  "category": "budget",
  "readOnly": false,
  "idempotent": true,
  "affectsAdSpend": true,
  "requiresConfirmation": true,
  "destructive": false
}
```

Field semantics:

- `id` — must match the argparse subcommand name in the skill's Python script. The orchestrator's `match_command_to_action` tokenizes the model's command via `shlex` and looks up the first non-flag token after the `.py`/`.sh` script path.
- `title` — human-readable label for confirmation cards / admin workbench.
- `category` — grouping tag (`diagnostics`, `reporting`, `persistence`, `campaign`, `budget`, `keyword`, `adset`, `ad`).
- `readOnly` — true means "no side effects anywhere," not just "doesn't change customer state." `save_snapshot` writes Firestore so it's `readOnly: false` even though it doesn't touch the customer's Google Ads account.
- `idempotent` — re-running with the same args is a no-op. Status flips (pause/enable) and `update_*` are idempotent; `add_*` and `create_*` are not.
- `affectsAdSpend` — flips on for any action that pauses, resumes, creates, or modifies an ad-platform campaign or budget. Surfaces in confirmation cards as a money-attention signal.
- `requiresConfirmation` — currently only logged (permissive phase); will gate execution behind a Firestore-backed confirmation flow in a later phase (planned, not yet documented).
- `destructive` — true for irreversible removals (`remove_campaign`, `remove_keywords`).

## Permissive validation phase

Phase 2 from the [[lynx-quality-architecture|build order]]. The orchestrator does NOT block on validation errors yet — it only logs and tags the tool envelope:

```python
# orchestrator/main.py — run_command branch
if get_skill_actions(skill_name):
    matched_action = match_command_to_action(skill_name, command)
    if matched_action:
        logger.info("Action matched: skill=%s action=%s", skill_name, matched_action["id"])
    else:
        action_gap = True
        logger.warning("Action gap: skill=%s command=%r — declare in manifest", ...)
```

The `meta.action` (or `meta.action_gap: true`) propagates into the [[tool-envelope]] returned to Claude. After a 14-day burn-in window with zero action_gap warnings across all production skills, strict mode flips on (refuses undeclared actions). See `commit 0c430aa` and `commit 3314285`.

## Skill index render

`build_skill_index` builds the model-facing menu. With manifest fields populated:

```
## Available Skills
- **bigcommerce** — BigCommerce store management
  _when: When the user asks about actual store/order facts: 'revenue today', 'sales today'... NOT for channel/source attribution (use ga4)..._
  _config: account_name=bannernprint_
- **ga4** — Google Analytics 4 traffic, revenue, ROAS, and attribution reporting
  _when: When the user asks about site analytics: traffic, sessions..._

Call `describe_skill(name='<skill>')` to see the full command list before using `run_command` on that skill.
```

Skills with `disableModelInvocation: true` are filtered out — they remain in the org's enabled list (for config UI) but are invisible to the model. `crm-notes` is the first skill to use this — it has no executable script; note-writing happens via the orchestrator's [[hermes-memory-system|Hermes pipeline]].

## Manifest cache

`load_agent_manifest` is process-lifetime cached (`_MANIFEST_CACHE` dict). At ~25 enabled skills × multiple call sites per turn (skill index, invocability check, action match, describe_skill), uncached disk reads add up. `clear_manifest_cache()` is exposed for tests/dev.

## What we did NOT adopt from Claude Code

- **Per-action typed tools.** Claude Code uses ONE `SkillTool` with `{skill, args}` — same shape as our `run_command`. Generating per-action tool definitions across 60+ actions would push the per-turn tool list to 30–140k input tokens.
- **`hooks` frontmatter.** No hook system in our orchestrator yet; deferred until there's a use case.
- **`context: fork` + `agent`.** No subagent forking infrastructure; deferred.

## Files

- `orchestrator/skill_loader.py` — manifest reader + cache + matcher
- `orchestrator/main.py` — dispatcher integration + describe_skill + run_command gating
- `orchestrator/anthropic_client.py` — `DESCRIBE_SKILL_TOOL` definition
- `.claude/skills/<skill>/agent.json` (in `grantllama-scrape-skill` repo) — per-skill manifest

## Related

- [[lynx-quality-architecture]] — the umbrella plan covering this + prompt cache + extended thinking + tool envelope
- [[tool-envelope]] — how `meta.action` reaches Claude via the standardized envelope
- [[hermes-memory-system]] — how `crm-notes` works without a model-callable interface
- [[ARCHITECTURE]] — overall stack diagram

## History

- `cf23396` (zeon-relay) — manifest reader: whenToUse, disableModelInvocation, cache, run_command gate
- `f82775d` (grantllama) — whenToUse text on 5 high-traffic skills
- `0c430aa` (zeon-relay) — permissive `actions[]` validation in run_command
- `3314285` (grantllama) — google-ad-campaign declares 20 actions
- `8acd0ea` (grantllama) — ga4 / bigcommerce / meta-ad-campaign manifests + crm-notes hidden
