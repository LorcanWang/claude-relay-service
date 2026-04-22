---
title: "Skill Concurrency Model"
aliases: ["concurrency", "skill-races", "sandbox-concurrency"]
tags: [orchestrator, skills, concurrency, race-condition, executor]
updated: 2026-04-21
status: active
---

# Skill Concurrency Model

What concurrency guarantees the skill executor provides, what it doesn't, and the design rules every skill must follow to stay safe under multi-room / multi-user load.

## What's isolated per invocation

Every `execute_command` call in `orchestrator/executor.py`:
- Spawns a **fresh `subprocess.Popen(shell=False)`** in its own session (process group). On timeout, `SIGTERM`s the whole group, then `SIGKILL` after a grace.
- Sets per-process **RLIMIT_AS / RLIMIT_CPU**. Memory leaks in skill code can't take down the orchestrator.
- Builds a **clean env** from scratch — no leakage of one room's `LYNX_CONFIG_*` into another's run.
- Writes to **its own tmpdir** for attachments (Phase 10).

So per-invocation isolation is solid. What follows are the things skills SHARE despite that isolation.

## What's NOT isolated — design around it

### 1. The skill directory itself

Every invocation runs the same `python3 path/to/skill_dir/skill.py …`. If two concurrent runs both `open(SKILL_DIR / "state.json", "w")`, last writer wins — the other invocation's update is silently dropped.

**Fix pattern**: scope writable paths by `LYNX_ORG_ID` (always set by the orchestrator), or `LYNX_ROOM_ID` if per-room state matters:

```python
import os
from pathlib import Path

def _state_path():
    raw = os.environ.get("LYNX_ORG_ID", "").strip()
    safe = "".join(c for c in raw if c.isalnum() or c in "-_") or "default"
    return Path(__file__).parent / f"state.{safe}.json"
```

For brand-monitor's alert-dedup file see `state.<orgId>.json` — the canonical example.

### 2. External APIs the skill drives

Concurrent invocations can both make the same call — e.g. two scheduled jobs running google-ad-campaign on overlapping cadences may both attempt to update the same campaign. The sandbox can't stop that.

**Fix pattern**:
- Mark non-replayable actions `idempotent: false` in `agent.json` so the scheduler doesn't retry.
- For mutations, lean on the upstream API's **idempotency key** (`request_id` on most ad APIs).
- For "read-then-write" flows, read-time-stamp the source and write `If-Match`-style.

### 3. The orchestrator event loop (read this carefully)

`execute_command` is a sync function called from an `async def` handler. **FastAPI does NOT auto-threadpool it.** While a 30s scan runs, every other concurrent room's `/chat` request waits behind it on the same uvicorn worker.

This is a throughput limit, not a correctness bug — but worth knowing.

**Mitigations** (any one helps):
- Flag long actions `longRunning: true` so they go through `task_worker.py` (separate process, doesn't share the orchestrator's event loop).
- Wrap the executor call in `asyncio.to_thread(execute_command, …)` (~10 LOC change in main.py).
- Run uvicorn with `--workers N` (multi-process, but RAM cost).

Detailed discussion — see [[build-plan]] open items. Fix landed: **TBD**.

### 4. Long-running task worker

`task_worker.py` polls Firestore for queued tasks and claims them transactionally (`tasks.claim_task`). Multiple worker processes can be spun up safely — the txn ensures no double-claim. Each worker still suffers issues 1+2 above; the txn only protects task ownership, not what the skill does inside.

## Audit checklist for an existing skill

Before declaring a skill production-ready under concurrent load:

- [ ] No `open(path, "w" | "a" | "r+")` against an unscoped path inside `SKILL_DIR`?
- [ ] Cache files (`*.json`, `*.cache`, `*.lock`) keyed by `LYNX_ORG_ID` or `LYNX_ROOM_ID`?
- [ ] Atomic writes for any shared file (`write to .tmp, os.replace`)?
- [ ] External API mutations either idempotent or marked `idempotent: false`?
- [ ] No module-level mutable state assumed to persist across invocations?

## Known offenders (audit pending)

A pass over all skills in `~/grantllama-scrape-skill/.claude/skills/` for the patterns above hasn't been done yet. Candidates worth looking at first based on what they touch:

- `gmail`, `google-workspace`, `google-ad-campaign`, `meta-ad-campaign` — drive external mutating APIs.
- `bigcommerce` — caches inventory locally.
- `sellersprite`, `amazon-*` — scrape with rate limits and likely cache.

## Files

- `orchestrator/executor.py` — sandbox, RLIMITs, `LYNX_*` env injection, timeout enforcement.
- `orchestrator/task_worker.py` — durable task runner, Firestore-claimed.
- `orchestrator/main.py` — sync `execute_command` call site (event-loop blocker, see issue 3).
- `~/.claude/skills/brand-monitor/SCHEDULING.md` — per-skill checklist; canonical reference for new skills.

## Related

- [[skill-manifest-evolution]] — `actions[]`, `longRunning`, `idempotent` declarations.
- [[lynx-quality-architecture]] — sync vs durable execution paths.
- [[build-plan]] — open items including the `asyncio.to_thread` fix.
