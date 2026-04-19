---
title: "Build Plan & Status"
aliases: ["build-plan", "roadmap", "status", "todo"]
tags: [planning, roadmap, status]
created: 2026-04-19
updated: 2026-04-19
status: active
---

# Build Plan & Status

Living checklist of the Lynx/Hive evolution settled in the [10-round Codex review](lynx-quality-architecture). Update when phases ship or new issues surface so the next session picks up cleanly.

## Status legend
- ✅ shipped + verified on VPS/Vercel
- 🟡 shipped, awaiting field verification
- 🔄 in progress (actively iterating)
- ⏸  blocked or paused
- ⏳ planned, not started
- 🐛 known bug

---

## Phase 1 — Per-room model override

✅ shipped `e6770ec` (zeon)

Admin-only model dropdown in Hive edit dialog. Stores `chatRoom.model` in Firestore; chat route forwards as `anthropicConfig.model`; orchestrator already honors `req.anthropicConfig.model || DEFAULT_MODEL`. Options: Default Sonnet 4.6, Opus 4.7, Sonnet 4.6, Haiku 4.5.

User-facing test: open Hive edit dialog as admin → "Model" field visible.

---

## Phase 2 — Skill manifest + permissive action validation

✅ shipped across `cf23396` (relay), `f82775d` (grantllama), `0c430aa` (relay), `3314285` + `8acd0ea` (grantllama)

- `whenToUse` rendered in skill index
- `disableModelInvocation` filters skill from index + blocks describe_skill + refuses run_command
- `actions[]` declared on 4 skills (google-ad-campaign 20, ga4 11, bigcommerce 10, meta-ad-campaign 19) — 60 actions total with category/readOnly/idempotent/affectsAdSpend/requiresConfirmation/destructive metadata
- `crm-notes` hidden from model (passive-only background skill)
- Permissive validation: logs `Action matched` / `Action gap`, tags envelope `meta.action`/`meta.action_gap`. Does NOT block.
- Manifest cache (`_MANIFEST_CACHE`) avoids repeated disk reads

**Burn-in window**: ~14 days. Before flipping to strict mode (refusing undeclared actions), need zero `Action gap` warnings across all production skills for the window. Started `2026-04-19`.

See [[skill-manifest-evolution]].

---

## Phase 3 — Extended thinking on Opus turns

✅ shipped `f89f8d7` (relay)

`_opus_thinking_config` returns `{type:"enabled", budget_tokens}` only when model contains `"opus"`. Budget clamped below `max_tokens`. Thinking blocks captured in `stream.content` (signature preserved verbatim) but not yielded to the UI.

Env knob: `OPUS_THINKING_BUDGET=4096`.

---

## Phase 4 — Prompt caching

✅ shipped `f89f8d7` (relay) + observability `a6af1e0`

`build_system_prompt` returns segmented blocks. Block 0 (stable core) gets `cache_control: {type: "ephemeral"}`. Skill index, dynamic context, and Hermes memory are uncached tail. Stream usage logged with `cache_creation_input_tokens` / `cache_read_input_tokens` for verification:

```
grep "Stream usage" ~/claude-relay-service/logs/orchestrator.log | tail -5
```

---

## Phase 5 — Tool result envelope

✅ shipped `b78781c` + `0c430aa` (relay)

Standardized `{status, summary, data?, error?, stderr?, stdout?, meta?}` shape. Wrapper-key strip, over-budget pruning (drops stdout → stderr → data, never slices JSON), `meta.action`/`meta.action_gap` propagation. See [[tool-envelope]].

---

## Phase 6 — Confirmation flow (modal + Firestore-backed)

⏳ planned, not started

Per the 10-round review:
- Out-of-band modal (Option B from R7 review)
- Firestore `pendingActions` collection (`pendingActionId`, `argsHash`, `requesterUserId`, `expiresAt`, `confirmationNonce`, `status`)
- Per-user nonce — only the requesting user can confirm
- Reopen re-entry shows pending actions on next visit
- State machine: PENDING → CONFIRMED → EXECUTING → COMPLETED (or → CANCELLED / EXPIRED)
- Server-side authority — client-side `confirmed: true` alone is replayable

Triggers off `meta.requiresConfirmation: true` from the manifest (already declared).

Estimated: 3 days.

---

## Phase 7 — Durable task model (long-running skills)

⏳ planned, not started

Per R6 review:
- `actions[].long_running: true` opts the action into the durable path
- Returns `taskId` immediately, status persisted to Firestore
- New `task_worker.py` (separate process; do NOT extend `hermes_worker.py`)
- No kill/retry in v1 — just durability across tab close
- Frontend subscribes to task doc via Firestore listener

Estimated: 4 days.

---

## Phase 8 — Context drawer

⏳ planned, not started

Per R8 review. Top-3 surfaces (in priority):
1. Loaded memory snippets (with `matched_query` reason)
2. Pending confirmations
3. Active model

Implementation: orchestrator inlines a `context-debug` SSE event at start of each turn carrying the snapshot it just used. UI subscribes to the existing chat SSE stream.

Estimated: 3 days.

---

## Phase 9 — Strict-mode flip + retire runner

⏳ scheduled for ~2026-05-03 (14 days post burn-in start)

After zero `Action gap` warnings for 14 days:
- Flip undeclared-action mode to strict (refuse rather than log)
- Delete `runner/` directory entirely (no production caller per R5 verification)

---

## Open bugs

### 🐛 Streaming chat goes blank requiring refresh
- Multiple iterations attempted: `0d4b535` (tier 1: status-gate), `348a11e` (tier 2: structural protection with 10s grace).
- Codex flagged the parts.length signal as weak against AI SDK v6 in-place mutations (input-streaming → input-available, output-error replacing output with errorText).
- **Status check needed**: user reported on `2026-04-19` that streaming "no longer working" — unclear if this is the same bug post-348a11e or a new regression. Must reproduce before next attempt.
- **Codex's preferred long-term fix**: server-side message revision counter + explicit server-authoritative flag. Heavier than the heuristic but bulletproof.

### 🐛 Hidden skills' whenToUse never reaches the model
- Symptom: user asked Lynx to "log a CRM note", Lynx answered "I can't log CRM notes from this space" — completely missing that crm-notes runs automatically via the orchestrator's pipeline.
- Root cause: `build_skill_index` filters out skills with `disableModelInvocation: true` so their `whenToUse` text is never seen by the model. Lynx has no way to know about background skills' existence.
- **Fix**: add a `## Background Skills` section to the system prompt listing hidden skills' name + whenToUse, with explicit "you cannot call these — they run automatically" framing.
- Estimated: 30 min including Codex review.

### 🐛 Room framing makes Lynx feel narrow ("can't help" responses)
- Same incident as above: Lynx replied "the tools available here are focused on ads performance" instead of engaging helpfully.
- The in-platform guardrail copy `"Prefer app_action(navigate)..."` reads to the model as "stay in your lane." Combined with the missing background-skill visibility, Lynx refuses adjacent topics it could discuss.
- **Fix**: soften the system-prompt framing in `skill_loader.py` to make Lynx engage with general questions even when the matching skill isn't directly callable. Pair with the Background Skills section above.

### 🐛 (Pre-existing, out of scope) `tool-invocation` part type rendering is dead code
- `chat-message.tsx:116` checks `part.type === "tool-invocation"` but AI SDK v6 emits `tool-${toolName}` and `dynamic-tool` instead. Tool UI rendering is silently disabled.
- Documented in the streaming-fix commit message; deferred.

---

## Decisions to revisit later

- **Lower windowed render default 30 → 10?** User suggested. Trade-off: tighter perf budget vs. more "Load earlier" clicks. Decision pending field test of current 30.
- **Per-org `whenToUse` overrides?** Some orgs may want different trigger phrasing. Currently global per skill in `agent.json`. Defer until pain surfaces.
- **`affectsAdSpend` field surfaced where?** Declared in manifests but no consumer reads it yet. Wire into confirmation modal copy when phase 6 lands.

---

## Files touched in this evolution

### claude-relay-service (orchestrator)
- `orchestrator/main.py` — system prompt assembly, action validation, envelope, thinking, cache
- `orchestrator/skill_loader.py` — manifest reader, skill index, action matcher, cache
- `orchestrator/anthropic_client.py` — DESCRIBE_SKILL_TOOL, `_opus_thinking_config`, list-or-string system

### zeon-solution-ai (frontend)
- `app/api/chat/route.ts` — pass per-room model
- `app/hive/[id]/page.tsx` — admin model dropdown
- `components/chat/chat-interface.tsx` — Firestore merge with two-tier protection
- `components/chat/chat-message-list.tsx` — windowed rendering
- `components/chat/chat-input.tsx` — local input state
- `components/chat/chat-message.tsx` — React.memo with parts signature

### grantllama-scrape-skill
- `.claude/skills/<skill>/agent.json` — manifests for 5 skills

---

## Related

- [[lynx-quality-architecture]] — umbrella plan
- [[skill-manifest-evolution]] — phase 2 detail
- [[tool-envelope]] — phase 5 detail
- [[chat-streaming-merge]] — frontend race fixes
- [[hermes-memory-system]] — adjacent system

## Cadence

Update this doc whenever:
- A phase ships
- A bug is resolved or filed
- A decision changes
- The next session picks up
