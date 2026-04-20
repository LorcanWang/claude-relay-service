"""
Durable task store — Firestore-backed state machine for skill actions marked
`longRunning: true` in their manifest.

Today skill execution is synchronous inside the HTTP/SSE chat request: >60s
kills on timeout, tab-close kills the work. Durable tasks lift that:

  State machine:  queued → running → completed
                                   ↘ failed
                  queued → cancelled (future; not wired v1)

  running tasks emit a heartbeat so a sweeper can mark them failed if the
  worker dies — without heartbeat, a crashed worker would leave the task
  stuck in "running" forever.

  claim_queued_task() is transactional (read-inside-txn + flip queued→running)
  so duplicate redelivery from Firestore listeners can't cause double
  execution — same correctness primitive as pending_actions.claim_confirmed_
  for_execution (see R6/R7 Codex review).

  Completion is idempotent: the worker posts the chat result message FIRST
  (capturing resultMessageId), THEN marks the task completed referencing that
  id. If mark_completed is retried, it no-ops when resultMessageId is already
  set.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from hermes_store import _get_db

logger = logging.getLogger("tasks")

COLLECTION = "longRunningTasks"

# How long a running task can go without a heartbeat before the sweeper
# declares it dead and marks it failed. Conservative default: 5 min, since
# our heartbeat cadence is 30s and we want to tolerate brief GC pauses.
HEARTBEAT_STALE_SECONDS = int(os.environ.get("TASK_HEARTBEAT_STALE_SECONDS", "300"))

# Upper bound on wall-clock execution time inside a single worker claim.
# Each skill subprocess still has its own SKILL_TIMEOUT; this is the task-
# level ceiling so a hung skill can't pin a worker forever.
TASK_TIMEOUT_SECONDS = int(os.environ.get("TASK_TIMEOUT_SECONDS", "3600"))

# Sweeper cadence (worker-side) — how often to poll for queued tasks the
# listener may have missed and to check for stale heartbeats.
SWEEP_INTERVAL_SECONDS = int(os.environ.get("TASK_SWEEP_INTERVAL_SECONDS", "45"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _hash_args(skill: str, command: str) -> str:
    h = hashlib.sha256()
    h.update(skill.encode("utf-8"))
    h.update(b"\x00")
    h.update(command.encode("utf-8"))
    return h.hexdigest()[:32]


# ── Create ───────────────────────────────────────────────────────────────────


def create_task(
    *,
    org_id: str,
    user_id: str,
    room_id: str | None,
    session_id: str,
    skill: str,
    command: str,
    action_id: str,
    action_title: str,
    destructive: bool = False,
    affects_ad_spend: bool = False,
    skill_configs: dict | None = None,
    in_platform: bool = True,
    pending_id: str | None = None,
) -> dict | None:
    """Write a queued task doc. Snapshots skill_configs + in_platform so the
    worker runs against the exact intent the user/supervisor approved — same
    discipline as pending_actions.create_pending.

    `pending_id` is set when the task was spawned by a supervisor approval of
    a requires-confirmation action; lets us link pending↔task for UI closure.
    """
    db = _get_db()
    if db is None:
        logger.warning("Firestore unavailable; cannot create task")
        return None

    task_id = str(uuid.uuid4())
    now_iso = _now_iso()
    doc = {
        "id": task_id,
        "orgId": org_id or "",
        "userId": user_id or "",
        "roomId": room_id or None,
        "sessionId": session_id or "",
        "skill": skill,
        "command": command,
        "actionId": action_id or "",
        "actionTitle": action_title or "",
        "destructive": bool(destructive),
        "affectsAdSpend": bool(affects_ad_spend),
        "skillConfigsSnapshot": dict(skill_configs or {}),
        "inPlatformSnapshot": bool(in_platform),
        "argsHash": _hash_args(skill, command),
        "pendingId": pending_id or None,
        "status": "queued",
        "workerId": None,
        "heartbeatAt": None,
        "startedAt": None,
        "completedAt": None,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        # Completion fields — filled by worker.
        "result": None,
        "resultMessageId": None,
    }
    try:
        db.collection(COLLECTION).document(task_id).set(doc)
    except Exception as exc:
        logger.error("create_task: Firestore write failed: %s", exc)
        return None
    logger.info(
        "Task queued: id=%s skill=%s action=%s user=%s room=%s pending=%s",
        task_id, skill, action_id, user_id, room_id, pending_id,
    )
    return doc


# ── Claim (worker side) ──────────────────────────────────────────────────────


def claim_queued_task(*, worker_id: str) -> dict | None:
    """Atomically claim one queued task for this worker. Returns the claimed
    doc (status flipped to 'running') or None if nothing eligible.

    Correctness contract — mirrors claim_confirmed_for_execution:
      1. Query candidates outside the txn to keep the txn small.
      2. Inside the txn, re-read by id and ONLY flip if status is still
         'queued'. Firestore listeners can redeliver stale snapshots, and
         concurrent workers race — the txn is the only safe claim point.
    """
    db = _get_db()
    if db is None:
        return None

    try:
        snap = (
            db.collection(COLLECTION)
            .where("status", "==", "queued")
            .order_by("createdAt", direction="ASCENDING")
            .limit(10)
            .get()
        )
    except Exception as exc:
        logger.warning("claim_queued_task: query failed: %s", exc)
        return None

    candidates: list[str] = []
    for d in snap:
        data = d.to_dict() or {}
        tid = data.get("id") or d.id
        if tid:
            candidates.append(tid)

    if not candidates:
        return None

    from firebase_admin import firestore as _fs

    for task_id in candidates:
        ref = db.collection(COLLECTION).document(task_id)
        outcome: dict = {"ok": False}

        try:
            transaction = db.transaction()

            @_fs.transactional
            def _txn(txn):
                snap2 = ref.get(transaction=txn)
                if not snap2.exists:
                    outcome.clear(); outcome["ok"] = False
                    return
                doc2 = snap2.to_dict() or {}
                if doc2.get("status") != "queued":
                    outcome.clear(); outcome["ok"] = False
                    return
                now_iso = _now_iso()
                txn.update(ref, {
                    "status": "running",
                    "workerId": worker_id,
                    "startedAt": now_iso,
                    "heartbeatAt": now_iso,
                    "updatedAt": now_iso,
                })
                doc2["status"] = "running"
                doc2["workerId"] = worker_id
                doc2["startedAt"] = now_iso
                doc2["heartbeatAt"] = now_iso
                outcome.clear(); outcome["ok"] = True; outcome["doc"] = doc2

            _txn(transaction)
        except Exception as exc:
            logger.warning("claim_queued_task: txn failed for id=%s: %s", task_id, exc)
            continue

        if outcome.get("ok"):
            logger.info("Task claimed: id=%s worker=%s", task_id, worker_id)
            return outcome["doc"]

    return None


def heartbeat(task_id: str) -> None:
    """Bump heartbeatAt. Called periodically by the worker while a task runs
    so the stale-running sweeper can tell live workers from dead ones.
    Best-effort — a skipped heartbeat is not fatal until HEARTBEAT_STALE_SECONDS.
    """
    db = _get_db()
    if db is None:
        return
    try:
        db.collection(COLLECTION).document(task_id).update({
            "heartbeatAt": _now_iso(),
        })
    except Exception as exc:
        logger.debug("heartbeat failed for id=%s: %s", task_id, exc)


# ── Completion (idempotent) ──────────────────────────────────────────────────


def mark_completed(
    task_id: str,
    *,
    result: dict,
    result_message_id: str | None = None,
) -> bool:
    """Flip running → completed with the result payload. If resultMessageId
    is already set (prior retry succeeded), this is a no-op on the message
    field — preserves idempotency for the "post message then mark completed"
    sequence the worker uses.
    """
    db = _get_db()
    if db is None:
        return False
    ref = db.collection(COLLECTION).document(task_id)
    try:
        from firebase_admin import firestore as _fs
        transaction = db.transaction()
        outcome = {"ok": False}

        @_fs.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            doc = snap.to_dict() or {}
            if doc.get("status") in ("completed", "failed", "cancelled"):
                outcome["ok"] = True  # already terminal, idempotent success
                return
            update = {
                "status": "completed",
                "completedAt": _now_iso(),
                "updatedAt": _now_iso(),
                "result": result,
            }
            # Only set resultMessageId if caller provided one AND doc doesn't
            # already carry one. Prevents a retry from clobbering the first
            # successful message id.
            if result_message_id and not doc.get("resultMessageId"):
                update["resultMessageId"] = result_message_id
            txn.update(ref, update)
            outcome["ok"] = True

        _txn(transaction)
        return bool(outcome.get("ok"))
    except Exception as exc:
        logger.error("mark_completed failed id=%s: %s", task_id, exc)
        return False


def mark_failed(task_id: str, *, error: str, result_message_id: str | None = None) -> bool:
    """Flip running → failed with an error summary. Idempotent like mark_completed."""
    db = _get_db()
    if db is None:
        return False
    ref = db.collection(COLLECTION).document(task_id)
    try:
        from firebase_admin import firestore as _fs
        transaction = db.transaction()
        outcome = {"ok": False}

        @_fs.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            doc = snap.to_dict() or {}
            if doc.get("status") in ("completed", "failed", "cancelled"):
                outcome["ok"] = True
                return
            update = {
                "status": "failed",
                "completedAt": _now_iso(),
                "updatedAt": _now_iso(),
                "result": {"ok": False, "error": error[:2000]},
            }
            if result_message_id and not doc.get("resultMessageId"):
                update["resultMessageId"] = result_message_id
            txn.update(ref, update)
            outcome["ok"] = True

        _txn(transaction)
        return bool(outcome.get("ok"))
    except Exception as exc:
        logger.error("mark_failed failed id=%s: %s", task_id, exc)
        return False


def record_result_message_id(task_id: str, message_id: str) -> None:
    """Attach a chat message id to the task doc WITHOUT transitioning status.
    Used when the worker posts the result message before the final state
    transition — lets mark_completed/failed be retried without double-posting.
    """
    db = _get_db()
    if db is None:
        return
    try:
        from firebase_admin import firestore as _fs

        ref = db.collection(COLLECTION).document(task_id)
        transaction = db.transaction()

        @_fs.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            doc = snap.to_dict() or {}
            if doc.get("resultMessageId"):
                return  # already recorded, no-op
            txn.update(ref, {
                "resultMessageId": message_id,
                "updatedAt": _now_iso(),
            })

        _txn(transaction)
    except Exception as exc:
        logger.warning("record_result_message_id failed id=%s: %s", task_id, exc)


# ── Sweeper (listener-drop + crash recovery) ─────────────────────────────────


def sweep_stale_running() -> list[str]:
    """Find running tasks whose heartbeatAt is older than HEARTBEAT_STALE_SECONDS
    and mark them failed. Returns the ids that were swept.

    This is the only mechanism that recovers from a worker crash. Without it,
    a SIGKILL'd worker leaves tasks stuck running forever.
    """
    db = _get_db()
    if db is None:
        return []
    cutoff = _now() - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    cutoff_iso = cutoff.isoformat()
    swept: list[str] = []
    try:
        # Candidates: status=running AND heartbeatAt<cutoff. We order by
        # heartbeatAt so old ones come first — bounded scan.
        snap = (
            db.collection(COLLECTION)
            .where("status", "==", "running")
            .where("heartbeatAt", "<", cutoff_iso)
            .limit(20)
            .get()
        )
    except Exception as exc:
        logger.warning("sweep_stale_running: query failed: %s", exc)
        return []

    for d in snap:
        data = d.to_dict() or {}
        tid = data.get("id") or d.id
        if not tid:
            continue
        if mark_failed(
            tid,
            error=(
                f"worker heartbeat stale (last at {data.get('heartbeatAt')}); "
                "task presumed dead and swept"
            ),
        ):
            swept.append(tid)
            logger.warning(
                "Swept stale task: id=%s worker=%s lastHeartbeat=%s",
                tid, data.get("workerId"), data.get("heartbeatAt"),
            )
    return swept


def list_queued(limit: int = 20) -> list[dict]:
    """Periodic fallback for the main listener — catches anything the
    realtime stream missed (e.g. after a listener reconnect)."""
    db = _get_db()
    if db is None:
        return []
    try:
        snap = (
            db.collection(COLLECTION)
            .where("status", "==", "queued")
            .order_by("createdAt", direction="ASCENDING")
            .limit(limit)
            .get()
        )
        return [d.to_dict() or {} for d in snap]
    except Exception as exc:
        logger.warning("list_queued failed: %s", exc)
        return []


# ── Reads ────────────────────────────────────────────────────────────────────


def load_task(task_id: str) -> Optional[dict]:
    db = _get_db()
    if db is None:
        return None
    try:
        snap = db.collection(COLLECTION).document(task_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data["id"] = task_id
        return data
    except Exception as exc:
        logger.warning("load_task failed id=%s: %s", task_id, exc)
        return None


def list_for_requester(org_id: str, user_id: str, limit: int = 20) -> list[dict]:
    """Tasks the user started. Used by a future /tasks outbox UI."""
    db = _get_db()
    if db is None:
        return []
    try:
        snap = (
            db.collection(COLLECTION)
            .where("orgId", "==", org_id)
            .where("userId", "==", user_id)
            .order_by("createdAt", direction="DESCENDING")
            .limit(limit)
            .get()
        )
        return [d.to_dict() or {} for d in snap]
    except Exception as exc:
        logger.warning("list_for_requester failed: %s", exc)
        return []
