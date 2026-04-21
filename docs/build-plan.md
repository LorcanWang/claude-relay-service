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

## Phase 6 — Confirmation flow (supervisor signoff dashboard)

✅ shipped (6a–6c, 4 commits per repo)

### 6a — Backend gate ✅ shipped `7cf973e`
- New `orchestrator/pending_actions.py` module with create/confirm/cancel/list/mark_executing/mark_completed
- Dispatcher branches on `matched_action.requiresConfirmation`, writes Firestore `pendingActions/{id}` with server nonce + per-user binding + argsHash + 30-min TTL
- Returns envelope `status: "awaiting_confirmation"` with `pending` field for UI
- New endpoints: `POST /pending-actions/{id}/confirm`, `POST .../cancel`, `GET /pending-actions?orgId&userId`
- `confirm()` uses Firestore transaction (concurrent confirms can't both succeed)
- `list_pending_for_user` redacts `nonce` field
- Fail-safe: store unavailable → dispatcher returns error, never executes ungated

### 6c — Supervisor signoff dashboard ✅ shipped (zeon `b9c4802`/`5a4caa3`/`23d5a9a` + relay `17dcb7c`/`7ec5114`)

Major architectural shift: per-user nonce model replaced with supervisor-membership model (user feedback: "imagine an employee trying to increase budget without approval — the approval place should be on a different dashboard, only the user assigned as supervisor of the room can approve").

- Schema: `chatRoom.supervisorUserIds` snapshot onto each pending at create time. Backfill script applied for prod rooms (BNP + AALYN both default to creator = Bruce).
- Backend (`pending_actions.py`):
  - `confirm()` validates supervisor membership (snapshot), bans self-approval on high-stakes (affectsAdSpend OR destructive)
  - `cancel()` allows requester or supervisor (dual-path, records cancelledByRole)
  - `claim_specific_for_execution(id)` — atomic claim by id for supervisor approve handler
  - `list_pending_for_supervisor` / `list_pending_for_requester`
  - Snapshots `skillConfigsSnapshot` so approve runs against requester's intended account
- Backend (`main.py`):
  - `POST /pending-actions/{id}/confirm` — atomic claim then `execute_command` server-side. Re-checks current room's enabled skills (revokes if removed). Returns `{ok, executed, executionOk, executionRevoked}`. No chat resume needed.
  - Drops nonce, adds `requesterUserId` to data-action payload for the read-only chat card.
- Frontend:
  - Inline card → read-only with "Open Sign-off" link + requester-only "Cancel my request"
  - `/hive/signoff` page with live Firestore sync, grouped by room, Approve/Cancel UI
  - "Sign-off" button on Hive list header
  - Admin-only "Supervisors" UserSearchSelect in room edit dialog
  - Hard-delete sweeps non-terminal pendingActions for the room
- Firestore: new indexes on pendingActions (supervisor / requester / room queries) + rules (read for supervisor or requester; writes server-only)
- Defense-in-depth: room PUT route strips supervisorUserIds from body if caller isn't admin
- Self-approval ban: enforced server-side in `confirm()` AND surfaced in UI via disabled Approve button when caller IS the requester on a high-stakes action

### 6b — Frontend card + resume execution ✅ shipped `2d2c905` (relay) + `b79dcc1` (zeon)
- Backend resume: new `claim_confirmed_for_execution` does an atomic Firestore txn check-confirmed-and-flip-to-executing (replaces the racy find+blind-update). `cancel` also transactional now.
- Next.js proxy routes: `app/api/pending-actions/[id]/confirm` and `.../cancel`. userId derived from auth (never trusted from client); nonce passes through to orchestrator.
- `pending-action-card.tsx` (NEW): inline card inside the assistant message bubble with action title, command preview, destructive/affectsAdSpend badges, Approve/Cancel buttons.
- `chat-message.tsx`: `_extractPendingActions` walks parts (handles v6 + legacy shapes), renders card per match.
- `chat-interface.tsx`: `handlePendingApproved` auto-sends "Approved — please proceed with X." via useChat → orchestrator's resume path executes transparently.
- UI copy: "Approved — running now…" (not "next turn", since auto-fires).
- Codex two-pass review: af6221d6923b3bb13 → fix-first on race + cancel txn → a007edc8643e8daea → commit.

### 6c — Re-entry surface ✅ shipped `100a4f6`
- `PendingReentryBanner` subscribes to pendingActions via Firestore client SDK. Rules already allow requester reads, so no server round-trip. Query: orgId+userId+status in [pending, confirmed]; roomId filtered client-side (per-room queue is small).
- Live-dismisses when supervisor approves/cancels — listener drops rows that transition out of pending/confirmed.
- Amber styling matches the inline card. Click → `/hive/signoff`. The inline card stays the primary affordance; this banner is the fallback for "scrolled away / closed tab" case.

### 6d — Post-approval feedback + audit trail ✅ shipped (relay `cd5f386` + zeon `dbe30e9`)
Closed two gaps that surfaced after 6c went live:
1. **Chat went dead after sign-off.** When a supervisor approved from `/hive/signoff`, the orchestrator executed server-side but nothing wrote back to the room. Chat sat at "Queued — awaiting confirmation" forever (even after refresh).
2. **No audit trail.** No way to answer "who approved what, when" — Hermes had no approval memory type.

Fix landed in `pending_actions.py`:
- `post_room_approval_message()` — appends a synthetic assistant message to `chatRooms/{roomId}/messages` using the same shape as `saveRoomMessages`. Chat's existing Firestore `onSnapshot` listener re-renders live, no refresh. Five outcomes: approved_executed / approved_failed / approved_revoked / cancelled / expired, each attributing the actor by displayName.
- `write_approval_memory()` — persists a new `memoryType: "approval_decision"` with `actorRefs` (approver + requester), skill, command fingerprint, action id, flags (affectsAdSpend/destructive), outcome. Non-fatal on write failure — the approval itself already committed transactionally.
- `load_pending()` helper since `cancel()` only returns ok/error; re-hydrates the doc so side-effects have full metadata.
- Wired into `/confirm` (all three branches) and `/cancel`.

Frontend side (`hermes-insights` refactor):
- **Campaigns → Intelligence** — query widened to include `insight`, `strategy_memory`, `workflow_pattern` alongside `campaign_*`. The old name was ads-agency-specific; non-ads verticals now see their own signal stream in the same tab. `?type=campaigns` kept as legacy alias.
- **New Approvals tab** — cards per approval with actor, skill, action, flags, outcome, timestamp. Tone-coded: green=approved, amber=failed/revoked, muted=cancelled.
- `TYPE_LABELS` / `TYPE_ICONS` register `approval_decision` so the Memories tab renders them consistently too.

No new Firestore indexes — existing `orgId + memoryType + status + temporal.lastSeenAt` composite covers both new queries.

---

## Phase 7 — Durable task model (long-running skills)

✅ shipped (relay `0a3a082` + zeon `fa3e50b`)

**What it solves.** Today `execute_command` is synchronous inside the HTTP/SSE chat request — >60s kills on SKILL_TIMEOUT, tab-close kills the work. Durable tasks mean actions marked `longRunning: true` in their manifest return a `taskId` immediately, then a separate worker process runs them to completion with status persisted to Firestore.

**Backend (`0a3a082`):**
- `tasks.py` — Firestore `longRunningTasks` state machine `queued → running → completed | failed`. `claim_queued_task` is transactional (read-inside-txn, flip only if still queued) — mirrors `claim_confirmed_for_execution`. Heartbeat + `sweep_stale_running` recovers from worker crashes (Codex said must-have-v1, not deferred). `mark_completed`/`mark_failed` idempotent via `resultMessageId`.
- `task_worker.py` — separate process (not extending hermes_worker.py; different reliability model — hermes drops, tasks must never drop). Listener + periodic sweep as drop insurance. Heartbeats every 30s. Re-runs revocation check at execution time. Uses `skillConfigsSnapshot`/`inPlatformSnapshot`, never live request context. Posts chat result FIRST (records messageId), THEN marks task terminal — retries can't double-post.
- `room_messages.py` — shared `post_synthetic_assistant_message` helper used by Phase 6d approvals AND Phase 7 task results. Txn reads `room.messageCount`, writes message with that index, bumps counter — atomic. Fixes pre-existing duplicate-index race in `post_room_approval_message`.
- `executor.execute_command` — new `timeout_seconds` override. Without it, module-level `SKILL_TIMEOUT` (60s) silently capped task_worker subprocesses regardless of `TASK_TIMEOUT_SECONDS`.
- `main.py` — dispatcher branches: `longRunning && !requiresConfirmation` enqueues a task and returns `awaiting_task`. `requiresConfirmation && longRunning`: `/confirm` spawns a task (linked to pending via `pending.taskId`) instead of blocking on sync execute. New outcome `approved_task_started` in approval chat copy + Hermes trail.
- `start.sh` — launches task_worker alongside orchestrator + hermes_worker. Existing launchd plist pulls in the new worker on restart, no new plist needed.

**Frontend (`fa3e50b`):**
- `task-status-card.tsx` — Firestore `onSnapshot` per taskId. Amber clock (queued) → blue spinner + elapsed (running) → green check (completed) | red X (failed). Copy explicitly tells the user they can close the tab.
- `chat-message.tsx` — `_extractTaskActions` walks parts for `data-action{action:"task"}`; renders card alongside any pending card on the same turn.
- `firestore.rules` — `longRunningTasks` read allowed to requester OR room supervisors. Writes server-only.
- `firestore.indexes.json` — three composite indexes: `(status, createdAt)` for claim, `(status, heartbeatAt)` for sweep, `(orgId, userId, createdAt)` for future outbox.

**Adoption.** To opt a skill into durable execution, add `"longRunning": true` to the relevant action in `agent.json`. That's the only change — the rest is automatic. Good first candidates: amazon-insights full-brand scrape, any bulk-campaign action.

**Intentional v1 cuts** (user + Codex agreed): no kill/retry button, no progress protocol beyond status, single worker. Multi-worker is safe because the claim txn is correct — just not wired yet.

**Deploy checklist:**
1. Push + `firebase deploy --only firestore:rules,firestore:indexes`
2. VPS: `bash orchestrator/launchd-fix.sh restart` to pick up task_worker
3. Opt in one skill manifest and test end-to-end

---

## Phase 10 — File attachments

✅ shipped (zeon `b35860a` → `c11df77` → `fa3e50b`, relay `3d40e3d` → `ef4941c` → `a5dad35`, grantllama `0177614` → `ca27ea1` → `506f59e`)

Bidirectional file support for hive chat. User uploads PDFs/images via paperclip; skills like `recruiting` can read them to extract structured data; skills can also emit files back (future use).

**Backend (zeon `b35860a`, `6586483`):** Firebase Storage `zeonsolutions` bucket, `hive-attachments/{orgId}/{roomId}/{attachmentId}/` prefix. Direct browser PUT via 15-min signed upload URL; Firestore `attachments/{id}` metadata doc is the authorization authority (per codex review — signed URLs are bearer-only). `/api/attachments/sign-upload`, `/api/attachments` (register), `/api/attachments/:id/download` (15-min read URL after room-member check). 7-day URL expiry; lifecycle rule deferred.

**Frontend (zeon `c11df77`):** Paperclip + multi-file selection in chat input with per-file upload chips (uploading/done/error, per-file remove). Send button blocks while uploads in flight. `attachment-chip.tsx` handles read-side: image preview loads lazily on click; everything else shows file icon + download button that mints a fresh signed URL. Uploads attach as `data-attachment` parts on the user message.

**Orchestrator (relay `3d40e3d`):** `attachments.py` resolves attachment refs → fetches Firestore doc → validates org/room scope → mints fresh 7-day signed GET URL via google-cloud-storage (same service account as firebase_admin). List injected as `LYNX_ATTACHMENTS_JSON` env var.

**Anthropic-native PDF/image (relay `ef4941c`):** PDFs become `{type:"document",source:{type:"url"}}` blocks, images become `{type:"image"}` blocks. Claude reads natively — no pdftotext/pypdf on VPS. Caps: 3 PDFs / 10 images per turn. Blocks scrubbed from persisted session messages after turn completes (text placeholder preserves filename) so follow-up turns don't re-ingest bytes.

**Executor sandbox (relay `a5dad35`):** Response to an incident where the model ran `brew install poppler` from inside a skill and the grandchild escaped the 60s timeout. Four layers: no shell (Popen with argv list via shlex), argv allowlist (python/python3 + .py file inside skill dir), env-var prefix allowlist (SKILL_ARGS_JSON only — blocks LYNX_ORG_ID impersonation), process-group kill on timeout (SIGTERM 5s → SIGKILL), Linux RLIMITs (NPROC/NOFILE/FSIZE/CPU) as defense in depth. Verified grandchild kill with a smoke test.

**First consumer (grantllama `0177614` → `ca27ea1`):** `recruiting` skill. Two invocation paths: (1) inline `parsed_records` in SKILL_ARGS_JSON — Claude parses PDF via document block, calls skill with pre-extracted fields; (2) legacy local-file path with `parsed_json`. `resume_import.py` now works without the PDF on disk — best-effort extraction, sha256 computed from text when no file, deterministic doc_id preserved via `candidate_id` field. Plus SSL certifi fix for the download path (matches bigcommerce 969bf05 pattern) and structured stderr logs `[phase10][recruiting]` at every decision point.

**Known gap (follow-up):** Attachment metadata snapshotting onto pendingActions / longRunningTasks docs. Needed for confirmation-gated or scheduled actions that carry attachments. Folded into Phase 11 prep.

---

## Phase 11 — Scheduled tasks

✅ shipped (zeon `46298c2` → `d928a49`)

Cron-driven runs of skill actions built on Phase 7's durable task infra. Vercel Cron hits a Next.js endpoint every minute; due schedules spawn either a `longRunningTask` (unlocked skills) or a `pendingAction` (confirmation-gated skills with deadline = next scheduled fire — unapproved → `expired_unapproved`).

**Backend (`46298c2`):**
- `schedules/{id}` Firestore collection with `cron`, `nextRunAt`, `lastRunStatus`, `requiresConfirmation` (snapshotted at create from action manifest), `skillConfigsSnapshot`, `inPlatformSnapshot`.
- `/api/cron/schedules` — Vercel Cron endpoint. Sweeps expired schedule-pendings, claims up to 50 due schedules per tick, spawns downstream docs. `CRON_SECRET` bearer auth.
- `claimDueSchedule` — Firestore transaction atomically advances `nextRunAt`. Miss-tick recovery fires ONCE for the latest missed window, advances past earlier missed ones (codex: "catch up all N" is a surprise-spend hazard). Deterministic task/pending ids `sch_{scheduleId}_{scheduledForIso}` — double-claim returns ALREADY_EXISTS and no-ops.
- `/api/schedules` CRUD — admin-gated. `sweepExpiredSchedulePendings` marks each unapproved scheduled pending as `expired_unapproved` once its deadline passes.

**Frontend (`d928a49`):** `/hive/schedules` page — list with status-colored badges, create dialog with cron presets (every-hour / 6h / daily / weekly / monthly) + freeform override, skill + action picker driven by `organization.enabledSkills`, auto-surfaces `requiresConfirmation` flag with supervisor banner. Admin-only create/edit/delete. Nav entry added under Hive dropdown (`add-hive-schedules-path.ts` migration).

**Deploy checklist:**
1. `firebase deploy --only firestore:rules,firestore:indexes --project zeon-solutions`
2. Vercel project: set `CRON_SECRET` env var; next deploy auto-registers the cron.
3. Test: create a schedule with a short cadence (e.g. every hour) against a safe skill action, watch `/hive/schedules` for `queued → completed` status.

**Deferred to future work:**
- Run history collection (today: only `lastTaskId` + `lastRunStatus` fields on the schedule doc). Next increment: `scheduleRuns/{id}` with per-fire audit.
- Per-schedule timezone (today: UTC only — add picker in create dialog).
- Manual trigger button ("fire now") in the UI — easy to add once we have an endpoint.

### Phase 11-redux — UX pivot to room-scoped schedules

✅ shipped (zeon `cfce236` → `afdc088` → `742fbab`, relay `9b5bde0`)

**What changed and why:** the original Phase 11 UI asked users to pick a skill, pick an action, and write a raw shell command (`SKILL_ARGS_JSON='...' python3 run.py`). Bruce screenshot-pushed back: too technical, wrong terminology ("skill" → "agent"), single-agent only, and wrong home (standalone page vs. room-internal). The redesign throws out the shell-exposure and recasts schedules as "re-run this natural-language prompt in this room on a cadence."

**Schema (`cfce236`):** Schedule doc drops `skill`, `actionId`, `command`, `requiresConfirmation`, `skillConfigsSnapshot`, `inPlatformSnapshot`. Adds `roomId` (required), `description`, `prompt`. Firestore rules tightened to creator-or-room-member reads; writes stay server-only.

**Execution path (`cfce236` + `9b5bde0`):** cron route resolves the schedule + room + org config via a new shared `buildScheduledChatRequest` helper (mirrors the live-chat resolution in `/api/chat/route.ts` exactly — same systemPrompt merge, enabledSkills, relay account binding). Config resolution happens at FIRE time, not create time (codex fold-in against stale-snapshot over-permission). The resolved ChatRequest is embedded in a `longRunningTask` with `kind: "scheduled_turn"`; the VPS `task_worker.py` branches on kind, POSTs it to the orchestrator's own `/chat` endpoint, and streams through to completion. All side effects (room chat messages, tool calls, confirmation gate, Hermes memory) happen inside the normal chat loop — no parallel machinery.

**Synthetic user turn** per codex: `role: "user"` with transparent prefix `[Scheduled run: <description> — fired at <iso>]`, `senderUserId: "schedule:<id>"`, `senderDisplayName: "Schedule · <desc>"`.

**UI (`afdc088`):** creation happens in-room. Hover over any user message → `🔁` icon opens the schedule dialog pre-filled with that text. Net-new via a `Schedule a prompt` button above the chat input (gated on `manage_hive`). Dialog takes three fields: short name, prompt (textarea, placeholder copy explains it's natural language), cadence (6 presets + Custom fallback). `/hive/schedules` becomes a read-only index grouped by room, with pause/resume/delete and click-through to open the originating space.

**Confirmation compose** (MVP limitation, not a feature): if the scheduled turn trips the confirmation gate mid-Lynx-chain, the stream ends early, `lastRunStatus` flips to `awaiting_confirmation`. Supervisor approval via `/hive/signoff` fires the single pending tool via the existing Phase 6 machinery, but the original Lynx chain-of-reasoning is not preserved. Future work: spawn a fresh scheduled turn with the approved tool result baked into the prefix.

**Deploy checklist (for this phase and the original Phase 11):**
1. `firebase deploy --only firestore:rules,firestore:indexes --project zeon-solutions` — new rules for schedules, scheduleRuns; new composite indexes.
2. Vercel: `CRON_SECRET` env var already set. No additional config beyond `vercel.json` cron entry.
3. VPS: `cd ~/claude-relay-service && git pull && bash orchestrator/launchd-fix.sh restart` — picks up the `kind: "scheduled_turn"` branch in `task_worker.py`.
4. Test: open a space, ask anything, hover the user message → 🔁 → dialog → pick "Every hour" → save. Wait up to 60s for cron to fire; watch the chat for the scheduled-run assistant turn and `/hive/schedules` for `queued → completed` status.

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
