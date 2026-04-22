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

### 3. The orchestrator event loop

`execute_command` used to be a sync function called from an `async def` handler — FastAPI did NOT auto-threadpool it, so a 30s scan in room A blocked every other concurrent `/chat` on the same uvicorn worker.

**Fix shipped 2026-04-22 (commit `452c37c`):**

- The two `/chat` call sites (`main.py:~1880` gap/confirm-resume, `main.py:~2010` normal tool call) now `await asyncio.to_thread(execute_command, …)`. Other rooms' turns no longer wait.
- The `/pending-actions/{id}/confirm` endpoint at `main.py:1203` is intentionally NOT wrapped — that handler is `def`, FastAPI auto-threadpools it.
- The resume path is wrapped in `asyncio.shield(_run_resume())` so a client disconnect mid-execution can't leave the pending stuck `executing`.
- A startup background task runs `pending_actions.sweep_stuck_executing()` every 60s as defence-in-depth — flips orphans to `failed`. `mark_completed` is now a transactional update with a `status==executing` precondition so a late thread can't clobber a sweeper-flipped doc.
- The default executor is capped at `EXECUTOR_THREADS=16` so unbounded `to_thread` bursts don't starve the host.

**Still on the to-do list:**

- Multi-worker uvicorn (`--workers N`) — blocked on moving `status_hub` to Redis pub/sub. The in-memory hub doesn't fan out across processes; SSE subscribers on worker A would miss events emitted by worker B.
- Other sync blockers Codex flagged (compaction `httpx.Client.post`, session Redis get/set, attachment Firestore/GCS, `pending_actions.claim_*` Firestore txns). Smaller per-case wins; address as they surface.

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
- [[build-plan]] — open items; multi-worker uvicorn rollout still pending.
