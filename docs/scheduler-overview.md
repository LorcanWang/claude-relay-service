---
title: "Scheduler Overview"
aliases: ["scheduler-overview", "unified-scheduler"]
tags: [scheduler, architecture, account-groups, sticky-session]
created: 2026-04-10
updated: 2026-04-13
status: active
---

# Scheduler Overview

The unified scheduler (`src/services/scheduler/unifiedClaudeScheduler.js`) is the core routing engine that selects which upstream account handles each request.

## Account Selection Priority

The scheduler follows this priority order:

1. **Dedicated account binding** — API Key has a specific account ID for the platform
2. **Group binding** — API Key is bound to `group:<groupId>`, scheduler picks from group members
3. **[[sticky-sessions|Sticky session]]** — session hash maps to a previously used account
4. **Pool selection** — priority-weighted selection from all available shared accounts

## [[account-types|Account Types]] in Groups

Claude platform groups can contain mixed account types:

| Type | `isActive` format | Status check |
|------|-------------------|--------------|
| `claude-official` | `'true'` (string) | `!= 'error' && != 'blocked'` |
| `claude-console` | `true` (boolean) | `== 'active'` |
| `ccr` | `'true'` (string) | `!= 'error' && != 'blocked'` (see [[ccr-scheduling-findings]]) |

## Availability Checks

After passing the status gate, accounts go through:

1. `isSchedulable` — account-level scheduling flag
2. `_isModelSupportedByAccount` — model compatibility
3. `isAccountTemporarilyUnavailable` — temporary 529/error cooldown
4. `isAccountRateLimited` — rate limit check
5. (CCR only) `isAccountQuotaExceeded` + `isAccountOverloaded` — added in PR #4
6. (Claude-official only) `isAccountOpusRateLimited` — Opus-specific limit
7. (Console only) concurrency limit check

## Performance Scoring

`src/services/scheduler/accountPerformanceService.js` provides an optional soft score (0-100) for each account based on operational insights data:

- Success rate (50% weight): from `ops:account:{id}:perf` Redis hash
- Latency tiers (30% weight): <2s = 30, <5s = 20, <10s = 10, >10s = 0
- Error recency (20% weight): no errors = 20, >1h ago = 15, >10min = 10, <10min = 0

Integrated into `sortAccountsByPriority` as an optional secondary signal after static priority but before lastUsedAt round-robin. Neutral score (50) when no data exists.

## Operational Insights

Every scheduler decision is recorded via `operationalInsightsService.recordSchedulerDecision()`:
- Selection method (dedicated, group, sticky, pool)
- Sticky hit/miss
- Account and type selected

Data feeds into hourly Redis rollups at `ops:hourly:{YYYY-MM-DD-HH}` with 72h TTL, queryable via `/admin/insights/scheduler`.
