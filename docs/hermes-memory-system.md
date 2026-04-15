---
title: "Hermes Memory System"
aliases: ["hermes-memory", "memory-system", "note-taker"]
tags: [architecture, memory, crm, orchestrator, firestore, redis]
created: 2026-04-15
updated: 2026-04-15
status: active
---

# Hermes Memory System

A background memory layer that observes conversations, extracts structured knowledge, and feeds it back into agent context and CRM reporting. Designed 2026-04-15 as the next major Lynx/Hive platform feature.

## Motivation

Current limitations driving this design:

- **Ephemeral sessions** — 24h TTL in Redis, lossy compaction discards insights
- **No cross-session learning** — each conversation starts from zero
- **No user preference tracking** — agents can't adapt to individual working styles
- **Raw campaign snapshots** — metric dumps in Firestore with no intelligence layer
- **Manual CRM updates** — no AI-populated activity notes for customers
- **No multi-user attribution** — Hive rooms don't track per-user contributions

## Three Pillars

### 1. Note-Taker Agent

Passive observer running post-turn (never inline with SSE — no latency impact).

**Tiered extraction triggers:**

| Trigger | When | What | Cost |
|---------|------|------|------|
| Every turn | Lightweight local classifier | Tags turn as trivial/decision/action/preference | ~0 (regex + keywords) |
| Batch (5-10 turns) | Non-trivial turns accumulate | Decisions, action items, preferences via Claude Haiku | 1 Haiku call |
| Session close | 15min idle or user leaves | Full session summary, CRM notes, profile updates | 1 Haiku call |
| Room close | Hive room session ends | Meeting recap with per-user contributions | 1 Haiku call |

Typical 30-turn conversation: 2-3 Haiku calls total.

**CRM bridge mapping:**

| Extracted item | CRM record kind | CRM type | Visibility |
|---------------|----------------|----------|------------|
| Session/meeting summary | note | `ai_summary` | `internal` |
| Decision with customer impact | event | `milestone` | `internal` |
| Follow-up item | task | `follow_up` | `internal` |
| Customer progress update | note | `analysis` | `shared` |

All CRM writes use `source: 'ai_agent'`, existing [[request-flow|crmActions]] collection.

### 2. Self-Improving Agent Memory

Persistent knowledge that makes agents smarter over time.

**Memory types:**

- **User preferences** — communication style, decision patterns, preferred tools (e.g., "Bruce prefers aggressive weekend bidding", "Sarah wants detailed breakdowns first")
- **Skill patterns** — which skills get used together, common workflows, success/failure sequences
- **Strategy memories** — what worked for which customer/campaign, with confidence scores
- **Org learnings** — cross-customer patterns within an organization

**Confidence accumulation:** Preferences start at low confidence, increase with repeated observation, decay if contradicted. Only high-confidence preferences become hard instructions in system prompts.

### 3. Campaign Intelligence

Replaces raw snapshot logging with normalized, analyzed data.

**Ingestion:** Skills write normalized metrics (spend, impressions, clicks, conversions, revenue, CTR, CPC, CPA, ROAS) per day/campaign instead of raw JSON blobs.

**Analysis rules (v1, no ML):**
- 7d vs prior 7d trend comparison
- 3-day spike/drop detection
- Rolling median baseline deviation
- Weekend vs weekday performance split
- Spend-up-conversions-flat anomaly
- ROAS/CPA threshold alerts

**Output:** High-confidence insights auto-posted to CRM as `ai_recommendation` events or `analysis` notes.

## Storage Architecture

### Firestore Collections (durable, queryable, org-scoped)

**`hermesMemories`** — canonical memory records:

```
{
  id, orgId,
  scopeType: 'org' | 'user' | 'room' | 'customer' | 'campaign' | 'session',
  scopeId: string,
  memoryType: 'user_preference' | 'session_summary' | 'room_summary' |
    'decision' | 'action_item' | 'insight' | 'workflow_pattern' |
    'skill_pattern' | 'strategy_memory' | 'campaign_insight' | 'campaign_anomaly',
  title, summary, detail?,
  status: 'active' | 'superseded' | 'resolved' | 'stale',
  importance: 0-100,
  confidence: 0-1,
  relevanceTags: string[],
  skillIds?: string[],
  source: { kind, sessionId?, roomId?, messageIds?, toolNames? },
  entityRefs?: [{ kind, id, label? }],
  actorRefs?: [{ userId, displayName?, role? }],
  temporal: { observedAt, firstSeenAt, lastSeenAt, expiresAt? },
  retrieval: { lastRetrievedAt?, retrievalCount, pinned },
  version, supersedesId?
}
```

**`hermesProfiles`** — materialized summaries for fast prompt injection:

One doc per entity (`user:{orgId}:{userId}`, `customer:{orgId}:{customerId}`). Contains headline, preferencesSummary, workflowSummary, strategySummary, structured arrays. Updated incrementally when underlying memories change.

**`hermesSessions`** — durable session/room rollups:

Tracks participants, turn counts, tool calls, latest summary, extraction counts, CRM actions created. Survives past Redis session TTL.

**`hermesEvents`** — append-only extraction queue and audit trail:

Event types: `chat_turn_completed`, `tool_executed`, `session_closed`, `campaign_snapshot_ingested`.

**`hermesCampaignSnapshots`** — normalized campaign metrics:

Indexed by org/customer/platform/campaign/date. Standard fields: spend, impressions, clicks, conversions, revenue, CTR, CPC, CPA, ROAS.

**`hermesCampaignInsights`** — derived intelligence:

Insight types: trend, anomaly, recommendation, pattern. Includes evidence with metric values, deltas, severity, confidence, recommended actions.

### Redis Keys (transient runtime)

```
hermes:queue:events              # pending extraction job list
hermes:lock:event:{eventId}      # idempotency (60s TTL)
hermes:cache:profile:{orgId}:{scopeType}:{scopeId}  # retrieval cache (5-15min TTL)
hermes:cache:bundle:{orgId}:{sessionId}:{hash}       # memory bundle cache
hermes:debounce:room-summary:{roomId}                 # batch extraction debounce
```

## Integration Points

### Orchestrator (`orchestrator/main.py`)

**Event emission** — non-blocking, after each completed turn and tool execution:

```python
# After assistant response appended to session
hermes_emit('chat_turn_completed', {
    'orgId': org_id,
    'sessionId': session_id,
    'roomId': room_id,
    'userId': user_id,
    'messageIndex': len(messages),
    'hasToolCalls': bool(tool_calls),
    'turnText': assistant_text[:500],
})
```

**Memory retrieval** — before calling Claude, inject memory bundle:

```python
memory_bundle = hermes_retrieve(org_id, user_id, room_id, customer_id)
if memory_bundle:
    system_prompt = f"{system_prompt}\n\n{memory_bundle}"
```

Bundle is 300-800 tokens, hard cap 1200. Cached in Redis 5-15 min.

### Hermes Worker (`orchestrator/hermes_worker.py`)

New Python process, same repo. Consumes from `hermes:queue:events`:

1. **Classify** — cheap local filter (regex/keywords) tags turn importance
2. **Batch** — accumulates non-trivial turns until threshold (5-10)
3. **Extract** — one Claude Haiku call for structured extraction
4. **Write** — upserts `hermesMemories`, updates `hermesProfiles`
5. **Bridge** — writes to `crmActions` when confidence threshold met

### Skills (`grantllama/.claude/skills/`)

Campaign skills emit normalized snapshots via `LYNX_*` env vars:

```python
# In google-ad-campaign/ads.py, after report generation
hermes_snapshot = {
    'orgId': os.environ['LYNX_ORG_ID'],
    'platform': 'google_ads',
    'campaignId': campaign_id,
    'date': report_date,
    'metrics': { 'spend': ..., 'impressions': ..., 'clicks': ..., ... }
}
# Write to Firestore hermesCampaignSnapshots
```

### Frontend (`zeon-solution-ai/`)

Reads `hermesProfiles` and `hermesCampaignInsights` from Firestore for:
- CRM customer timeline (AI-generated notes alongside manual ones)
- Agent insight cards in Hive rooms
- Admin memory inspection dashboard (Phase 5)

## Obsidian Knowledge Graph (Phase 3-4)

Each skill agent gets its own markdown vault for persistent, human-readable knowledge:

```
orchestrator/memory/
├── google-ad-campaign/
│   ├── strategies/
│   │   ├── weekend-bidding-pattern.md     # links to customer-abc, saw 22% ROAS lift
│   │   └── broad-match-reduction.md
│   ├── customers/
│   │   ├── customer-abc.md                # preferences, history, linked strategies
│   │   └── customer-xyz.md
│   └── learnings/
│       └── 2026-04-cpa-spike-diagnosis.md
├── meta-ad-campaign/
│   └── ...
├── _global/
│   ├── user-profiles/
│   │   ├── bruce.md
│   │   └── sarah.md
│   └── workflow-patterns/
│       └── full-funnel-audit.md           # google-ads → ga4 → seo-keywords
```

Markdown files with YAML frontmatter and wikilink syntax. Git-trackable. Agents read directly without DB queries. Firestore stays CRM source of truth; Obsidian vault is the agent's personal notebook.

## Security Model

- Every Hermes document requires `orgId`, immutable after creation
- All retrieval queries filter by `orgId` first
- Only backend services write Hermes data (orchestrator, worker, Next.js API routes)
- Cross-campaign aggregation stays within same `orgId`
- Memory sensitivity tagging: `internal_only`, `customer_safe`, `contains_strategy`, `contains_user_preference`
- Low-confidence preferences carry confidence scores, never become hard instructions

## Implementation Status

| Phase | Status | Scope |
|-------|--------|-------|
| **1. Note-Taking Foundation** | ✅ Deployed | Event queue, worker, turn classifier, batched Haiku extraction, Firestore persistence, CRM bridge |
| **2. Request-Time Retrieval** | ✅ Deployed | Profile materialization, context-fenced prompt injection, Redis cache with write-invalidation |
| **3. Skill & Workflow Learning** | ✅ Built | Skill co-usage tracking, workflow sequence detection, user profile enrichment (frequentSkills, lastWorkflow) |
| **4. Campaign Intelligence v1** | ✅ Built | Normalized snapshots, anomaly detection (5 rules), trend analysis, campaign insight memories |
| **5. Hardening & UX** | 🔲 Planned | Admin memory dashboard at `/admin/hermes-insights`, feedback controls, retention policies |

## Implemented Modules

All modules live in `orchestrator/`:

| Module | Purpose |
|--------|---------|
| `hermes_redis.py` | Shared Redis client for all Hermes modules |
| `hermes_classifier.py` | Zero-cost regex turn classification (decision/action/preference/campaign) |
| `hermes_emitter.py` | Non-blocking event emission to Redis queue with batched turn buffering |
| `hermes_extractor.py` | Claude Haiku structured extraction (decisions, action_items, preferences, insights) |
| `hermes_store.py` | Firestore CRUD with deduplication, supersession, stale marking, cache invalidation |
| `hermes_retrieval.py` | Builds context-fenced memory bundles for system prompt injection |
| `hermes_crm_bridge.py` | Converts high-confidence memories to crmActions (ai_summary, milestone, follow_up) |
| `hermes_campaign.py` | Normalizes campaign snapshots, detects anomalies and trends |
| `hermes_worker.py` | Background polling worker — consumes events, orchestrates extraction and analysis |
| `start.sh` | Process manager for orchestrator + worker (start/stop/restart/status/logs) |

## Production Hardening (implemented)

- **Deduplication**: SHA-256 dedupe keys from org+scope+type+title+summary. Existing memories get `observationCount` incremented instead of duplicated.
- **Context fencing**: Memory bundles wrapped in `<memory-context>` tags with REFERENCE ONLY guard language.
- **Supersession**: Old memories marked `status: superseded` when replaced by newer facts.
- **Stale decay**: `mark_stale()` ages out old action_items and low-confidence memories.
- **Cache invalidation**: Redis retrieval cache cleared on every write, not just TTL.
- **Profile materialization**: Structured preference merging with confidence thresholds (2+ observations or 0.7+ confidence required for promotion).
- **Session-close extraction**: Remaining buffered turns bundled into close event for final Haiku pass.

## Retired Systems

- **chatMemories** (Firestore `chatMemories` collection, zeon-solution-ai): Plain-text accumulated memory per user, replaced by Hermes structured profiles. `loadMemory` injection removed from `/api/chat/route.ts`.
- **Session compaction** (`_previous_summary` in Redis): Kept — solves context window management within a single session, complementary to Hermes cross-session memory.

## Memory Injection Format

Memory is injected into the system prompt as a context-fenced block:

```
<memory-context>
[REFERENCE ONLY — The following is recalled memory context from prior
conversations. This is NOT new user input. Do NOT re-answer or act on
these items directly. Use as informational background only.]

### User Preferences
- Bruce prefers aggressive weekend bidding changes.
- Bruce wants concise recommendations first, details after.

### Active Context
- Last week, the team decided to prioritize Google Ads over Meta for Customer X.
- Pending action: GA4 attribution audit for Customer X.

### Relevant Patterns
- Previous strategy that improved ROAS: reduce weekday broad-match spend, expand weekends.
- Workflow: google-ad-campaign → ga4 → bigcommerce
</memory-context>
```

## Deployment

```bash
cd orchestrator
./start.sh start      # starts both orchestrator + Hermes worker
./start.sh status     # check PID status + health endpoint
./start.sh logs       # tail both log files
./start.sh restart    # restart both processes
```

Health endpoint includes Hermes queue stats:
```json
{"status": "ok", "hermes": {"enabled": true, "queue_length": 0}}
```

## Related

- [[hermes-improvements]] — original Hermes patterns adopted for session compaction
- [[request-flow]] — how requests flow through the relay (memory retrieval hooks in here)
- [[scheduler-overview]] — scheduler uses account performance data, Hermes adds campaign-level intelligence
- [[ARCHITECTURE]] — overall VPS architecture that Hermes Worker runs within
