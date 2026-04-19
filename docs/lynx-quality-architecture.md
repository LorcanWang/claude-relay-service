---
title: "Lynx Quality Architecture"
aliases: ["lynx-quality", "quality-plan", "system-prompt-cache", "extended-thinking"]
tags: [architecture, orchestrator, claude, prompt-caching, anthropic, performance]
created: 2026-04-19
updated: 2026-04-19
status: active
---

# Lynx Quality Architecture

The umbrella plan for closing the quality gap between Lynx (relay orchestrator) and Claude Code (terminal). Settled in a 10-round Codex review and shipped over multiple commits in April 2026.

## The original gap

Lynx was running Sonnet 4.6 by default while Claude Code uses Opus 4.7. Beyond model choice:
- Skill docs inlined in full per turn (~15–25KB system prompt).
- No prompt caching — every turn re-sent the same stable prefix.
- Tool surface was a single generic `run_command(skill, command)` with no manifest awareness.
- Tool results came back in inconsistent shapes (success → bare data, failure → wrapped envelope).
- No extended thinking even for hard reasoning turns.
- Output capped at 8192 tokens — long analyses got truncated.

## Five-phase plan (settled)

| # | Change | Status | Commit |
|---|---|---|---|
| 1 | Per-room model override (admin-only) | shipped | `e6770ec` (zeon) |
| 2 | Skill index + `describe_skill` tool (lazy doc load) | shipped | `f89f8d7`, `cf23396` |
| 3 | Extended thinking on Opus turns only | shipped | `f89f8d7` |
| 4 | Prompt caching via `cache_control` on stable prefix | shipped | `f89f8d7` |
| 5 | Tool result envelope standardization | shipped | `b78781c`, `0c430aa` |

## System prompt layout (segments)

Built by `orchestrator/skill_loader.py:build_system_prompt`, returned as a list of `{type: "text", text: ..., cache_control?}` blocks:

```
[0] STABLE CORE                              ← cache_control: ephemeral
    base prompt
    ## App Actions (full contract)
    ## Using Skills (describe_skill instructions)

[1] SKILL INDEX (compact)                    ← per-room, no cache
    ## Available Skills
    - **bigcommerce** — ...
      _when: ..._
      _config: ..._
    ... + describe_skill prompt

[2] DYNAMIC TAIL                             ← per-turn, no cache
    ## Current User Context
    org_id / user_id / room_id / in_platform
    + room-mode multi-user note

[3] HERMES MEMORY BUNDLE (optional)          ← per-turn, no cache
    <memory-context>
      ### Pinned Strategies
      ### Active Context
      ### Cross-Room Signals
    </memory-context>
```

`main.py` attaches `cache_control: {type: "ephemeral"}` to block 0 only. The Anthropic prompt cache hashes the cached prefix; subsequent turns in the same room hit the cache for ~5 minutes. Net effect: drastic reduction in input-token cost for the cached-prefix portion + lower first-token latency.

Verify cache hits by grepping logs:
```
grep "Stream usage" ~/claude-relay-service/logs/orchestrator.log | tail -5
# cache_read_input_tokens > 0 on 2nd+ turn = cache hit
```

## Extended thinking

Claude Opus supports a `thinking` API parameter for explicit reasoning budget. `_opus_thinking_config` in `anthropic_client.py` returns `{type: "enabled", budget_tokens: N}` ONLY when `model.lower().contains("opus")`. Sonnet/Haiku turns stay lean. Budget is clamped below `max_tokens` to avoid Anthropic API errors.

Thinking blocks arrive as content-block type `thinking` in the SSE stream. The `AnthropicStream` parser captures them into `stream.content` (so they survive into `session["messages"]` for follow-up turns — Anthropic requires the signature to be passed back verbatim) but does NOT yield them to the client SSE — UI never sees thinking text.

Env knob: `OPUS_THINKING_BUDGET` (default 4096).

## Per-room model override

Admin-only dropdown in the Hive space edit dialog (`app/hive/[id]/page.tsx`). Stores `chatRoom.model` in Firestore. The chat route (`app/api/chat/route.ts`) reads it and includes `model` in `anthropicConfig` sent to the orchestrator. Orchestrator already respected `req.anthropicConfig.model || DEFAULT_MODEL` (`main.py:721`) — no server change needed.

Options exposed:
- Default (Sonnet 4.6)
- Opus 4.7 (~5× cost; smartest)
- Sonnet 4.6 (balanced)
- Haiku 4.5 (fastest/cheapest)

Visibility gated by `hasPermission("admin")` in the dialog; PUT body only includes `model` when `isAdmin`.

## Tool surface

Three tools exposed (run_command, describe_skill, app_action) when the room has skills enabled; only `app_action` when none. See [[skill-manifest-evolution]] for why we don't fan out per-skill or per-action — the unified surface mirrors Claude Code's `SkillTool` and avoids the 50–140k token-cost explosion of per-action tools across our ~25 skills × 5–12 subcommands each.

## Output token cap

`MAX_OUTPUT_TOKENS = 16384` (env-tunable, was 8192). Lets long analyses complete without `length`-stop truncation. Doubled cost on a single long turn vs. baseline; rare in practice.

## What this DOES NOT replace

- **Hermes Memory** — long-term knowledge graph, separate concern. See [[hermes-memory-system]].
- **Session compaction** — when message history exceeds 40 turns. See [[chat-streaming-merge]] for the streaming side.
- **Loop iteration cap** — `MAX_LOOP=50`. Hard ceiling on tool-call rounds per turn.

## Files

- `orchestrator/main.py` — system prompt assembly + cache_control attachment + thinking + max_tokens
- `orchestrator/anthropic_client.py` — `_opus_thinking_config`, `DESCRIBE_SKILL_TOOL`, `system: list | str` support
- `orchestrator/skill_loader.py` — `build_system_prompt` returns segmented blocks
- `app/api/chat/route.ts` (zeon) — passes per-room model override
- `app/hive/[id]/page.tsx` (zeon) — admin model dropdown

## Related

- [[skill-manifest-evolution]] — phase 2 detail
- [[tool-envelope]] — phase 5 detail
- [[chat-streaming-merge]] — frontend perf + race fixes that landed in parallel
- [[ARCHITECTURE]] — overall stack

## History

- `e6770ec` (zeon) — per-room model override (phase 1)
- `f89f8d7` (relay) — skill index + describe_skill + extended thinking + prompt caching (phases 2–4)
- `cf23396` (relay) — manifest reader (whenToUse, disableModelInvocation, cache)
- `b78781c` (relay) — tool envelope standardization (phase 5)
- `0c430aa` (relay) — permissive action validation
- `a6af1e0` (relay) — usage observability for cache verification
