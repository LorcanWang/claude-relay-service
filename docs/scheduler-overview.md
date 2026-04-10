---
title: "Scheduler Overview"
aliases: ["scheduler-overview", "unified-scheduler"]
tags: [scheduler, architecture, account-groups, sticky-session]
created: 2026-04-10
updated: 2026-04-10
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
| `ccr` | `'true'` (string) | See [[ccr-scheduling-findings]] |

## Availability Checks

After passing the status gate, accounts go through:

1. `isSchedulable` — account-level scheduling flag
2. `_isModelSupportedByAccount` — model compatibility
3. `isAccountTemporarilyUnavailable` — temporary 529/error cooldown
4. `isAccountRateLimited` — rate limit check
5. (Claude-official only) `isAccountOpusRateLimited` — Opus-specific limit
6. (Console only) concurrency limit check

See [[ccr-scheduling-findings]] for gaps in CCR-specific checks within the group path.
