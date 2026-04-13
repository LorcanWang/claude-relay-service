---
title: "Hermes Agent Improvement Plan"
aliases: ["hermes-improvements"]
tags: [architecture, scheduler, review, findings]
created: 2026-04-13
updated: 2026-04-13
status: active
---

# Hermes Agent Improvement Plan

Analysis of NousResearch/hermes-agent memory management patterns applied to Claude Relay Service. Reviewed 2026-04-13.

## What Was Built

### P0: Session Compaction Rework (`orchestrator/main.py`)

Replaced basic single-pass summarizer with Hermes-inspired structured compaction:

- **Boundary-aware splitting**: `_find_compaction_boundary()` never splits tool_use/tool_result pairs
- **Head protection**: first 2 messages always preserved as context anchors
- **Structured summary template**: 8 sections (Goal, Progress, Key Decisions, Resolved Questions, Pending User Asks, Relevant Data, Remaining Work, Critical Context)
- **Iterative updates**: `_previous_summary` stored in session; subsequent compactions update rather than replace
- **Tool result pruning**: pre-pass replaces >200 char tool results with placeholder before summarization
- **Injection scanning**: `_scan_summary()` blocks prompt injection patterns
- **Fallback marker**: static context marker when summary fails or is blocked

### P0: Operational Insights Service (`src/services/operationalInsightsService.js`)

Redis-backed metrics with hourly rotation:

- **Request tracking**: counts, completions, errors, disconnects
- **Scheduler quality**: sticky hit/miss rate, selection method breakdown (dedicated/group/pool)
- **Per-account performance**: latency, success rate, error history (24h TTL)
- **Token usage**: input/output token totals per hour
- **Admin API**: 4 endpoints at `/admin/insights/{summary,hourly,scheduler,accounts}` (72h retention)

### P1: Scheduler Intelligence (`src/services/scheduler/accountPerformanceService.js`)

Performance scoring for scheduling decisions:

- Reads from `ops:account:{id}:perf` Redis hashes
- Score 0-100: success_rate (50%), latency (30%), error recency (20%)
- Integrated into `sortAccountsByPriority` as optional secondary signal
- Backward compatible — existing callers unaffected

## Hermes Patterns Adopted

1. **Frozen snapshot pattern** — system prompt uses state captured at session start
2. **Context fencing** — summary prefix with "REFERENCE ONLY, do NOT re-answer"
3. **Boundary-aware operations** — never split logical tool groups
4. **Iterative refinement** — update previous summary, don't regenerate from scratch
5. **Structured schemas** — explicit sections instead of freeform prose
6. **Injection scanning** — block prompt injection in stored summaries

## Hermes Patterns Skipped

- MemoryManager plugin orchestration (relay is middleware, not agent)
- MemoryProvider/BuiltinMemoryProvider ABC (only one memory store)
- SessionSearchTool FTS5 (conversation logs too lossy for search)
- CheckpointManager shadow git repos (no editable working memory)
- Full InsightsEngine agent analytics (operational telemetry fits better)

## Related

- [[ccr-scheduling-findings]]
- [[scheduler-overview]]
- [[request-flow]]
