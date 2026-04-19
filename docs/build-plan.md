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

🔄 in progress

### 6a — Backend gate ✅ shipped `7cf973e`
- New `orchestrator/pending_actions.py` module with create/confirm/cancel/list/mark_executing/mark_completed
- Dispatcher branches on `matched_action.requiresConfirmation`, writes Firestore `pendingActions/{id}` with server nonce + per-user binding + argsHash + 30-min TTL
- Returns envelope `status: "awaiting_confirmation"` with `pending` field for UI
- New endpoints: `POST /pending-actions/{id}/confirm`, `POST .../cancel`, `GET /pending-actions?orgId&userId`
- `confirm()` uses Firestore transaction (concurrent confirms can't both succeed)
- `list_pending_for_user` redacts `nonce` field
- Fail-safe: store unavailable → dispatcher returns error, never executes ungated

### 6b — Frontend card + resume execution ✅ shipped `2d2c905` (relay) + `b79dcc1` (zeon)
- Backend resume: new `claim_confirmed_for_execution` does an atomic Firestore txn check-confirmed-and-flip-to-executing (replaces the racy find+blind-update). `cancel` also transactional now.
- Next.js proxy routes: `app/api/pending-actions/[id]/confirm` and `.../cancel`. userId derived from auth (never trusted from client); nonce passes through to orchestrator.
- `pending-action-card.tsx` (NEW): inline card inside the assistant message bubble with action title, command preview, destructive/affectsAdSpend badges, Approve/Cancel buttons.
- `chat-message.tsx`: `_extractPendingActions` walks parts (handles v6 + legacy shapes), renders card per match.
- `chat-interface.tsx`: `handlePendingApproved` auto-sends "Approved — please proceed with X." via useChat → orchestrator's resume path executes transparently.
- UI copy: "Approved — running now…" (not "next turn", since auto-fires).
- Codex two-pass review: af6221d6923b3bb13 → fix-first on race + cancel txn → a007edc8643e8daea → commit.

### 6c — Re-entry surface ⏳ later
- On room load, query `GET /pending-actions` for non-terminal entries
- Show "you have N pending actions" banner with click-to-reopen

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

### ✅ Streaming chat goes blank requiring refresh — RESOLVED `b7607c0`
- Three prior fix attempts (`0824978`, `0d4b535`, `348a11e`) were all wrong layer.
- Real root cause (Codex agent a2801357ddea28045): AI SDK v6's `pushMessage`
  leaks the live mutable assistant message object into React state. By the
  time `React.memo`'s comparator runs, `prev.message` and `next.message` are
  the SAME already-mutated reference, so any signature/length compare sees
  equal and blocks every re-render forever.
- Fix `b7607c0`: pass `isStreaming` to the last assistant message during
  loading; comparator returns false when set, bypassing memo entirely for
  the streaming tail. All other messages still benefit from memo.
- Performance bundle `bb76549` (which introduced the bug) stays intact — we
  just unblock the one slot that needs to re-render on every delta.

### ✅ Hidden skills' whenToUse never reaches the model — RESOLVED `fc6bd3e`
- Was: `build_skill_index` filtered out skills with `disableModelInvocation: true` so their `whenToUse` text never reached the model. Lynx replied "I can't log CRM notes from this space."
- Fix `fc6bd3e`: new `build_background_skills_block()` emits a `## Background Skills` section listing hidden skills + whenToUse + explicit "do NOT say 'I can't do that here' — explain how the background pipeline handles it" guidance. run_command still refuses hidden skills via the existing `is_model_invocable` gate; this is purely informational.

### ✅ Room framing makes Lynx feel narrow — RESOLVED `fc6bd3e`
- Was: in-platform guardrail copy read as "stay in your lane." Combined with the missing background-skill visibility, Lynx refused adjacent topics.
- Fix `fc6bd3e`: softened the in-platform copy with explicit "the skills listed above are tools you happen to have, NOT the boundary of what you can discuss. Engage with whatever the user asks — general advice, follow-ups, planning, recommendations" framing. Only surface "I don't have a tool" when the user explicitly needs a tool action.

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
