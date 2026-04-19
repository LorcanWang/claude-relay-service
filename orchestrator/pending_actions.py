"""
Pending action store — Firestore-backed confirmation gate for actions
declared with `requiresConfirmation: true` in their skill manifest.

Every requires-confirmation tool call writes a doc here BEFORE executing.
The frontend renders a confirm/cancel UI; on approve, it POSTs to the
orchestrator's /pending-actions/{id}/confirm endpoint with the per-user
nonce. The orchestrator marks status=confirmed; the next chat turn
checks for confirmed-but-unexecuted entries and resumes.

Design (from the 10-round Codex review, R7 verdict):
  - Server-side authority — never trust client-side `confirmed: true` alone.
  - Per-user nonce — only the requesting user can approve.
  - Args hash binding — if the model changes args, the nonce becomes invalid.
  - State machine: PENDING → CONFIRMED → EXECUTING → COMPLETED
                                      ↘ CANCELLED
                       PENDING → EXPIRED (background sweeper / lazy)
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from hermes_store import _get_db

logger = logging.getLogger("pending_actions")

# How long an unconfirmed pending action stays valid. After this it's
# considered EXPIRED on lookup; the orchestrator must not execute it.
PENDING_TTL_MINUTES = int(os.environ.get("PENDING_ACTION_TTL_MINUTES", "30"))

COLLECTION = "pendingActions"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _hash_args(skill: str, command: str) -> str:
    """Stable fingerprint of (skill, command) so we can detect arg drift."""
    h = hashlib.sha256()
    h.update(skill.encode("utf-8"))
    h.update(b"\x00")
    h.update(command.encode("utf-8"))
    return h.hexdigest()[:32]


def create_pending(
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
) -> dict | None:
    """Create a pending action doc. Returns the created record (with id+nonce)
    or None if Firestore is unavailable.
    """
    db = _get_db()
    if db is None:
        logger.warning("Firestore unavailable — cannot create pending action")
        return None

    pending_id = uuid.uuid4().hex
    nonce = secrets.token_urlsafe(24)
    now = _now()
    expires = now + timedelta(minutes=PENDING_TTL_MINUTES)

    doc = {
        "id": pending_id,
        "orgId": org_id,
        "userId": user_id,
        "roomId": room_id,
        "sessionId": session_id,
        "skill": skill,
        "command": command,
        "actionId": action_id,
        "actionTitle": action_title,
        "argsHash": _hash_args(skill, command),
        "destructive": bool(destructive),
        "affectsAdSpend": bool(affects_ad_spend),
        "nonce": nonce,
        "status": "pending",
        "createdAt": now.isoformat(),
        "expiresAt": expires.isoformat(),
        "confirmedAt": None,
        "confirmedBy": None,
        "executedAt": None,
        "result": None,
    }
    try:
        db.collection(COLLECTION).document(pending_id).set(doc)
        logger.info(
            "Pending action created: id=%s skill=%s action=%s user=%s",
            pending_id, skill, action_id, user_id,
        )
        return doc
    except Exception as exc:
        logger.warning("Failed to create pending action: %s", exc)
        return None


def get_pending(pending_id: str) -> dict | None:
    db = _get_db()
    if db is None:
        return None
    try:
        snap = db.collection(COLLECTION).document(pending_id).get()
        if not snap.exists:
            return None
        return snap.to_dict()
    except Exception as exc:
        logger.warning("get_pending failed: %s", exc)
        return None


def confirm(pending_id: str, *, nonce: str, user_id: str) -> dict:
    """Validate nonce + user + freshness, mark CONFIRMED. Returns
    {ok: bool, error?: str, pending?: dict}.

    Uses a Firestore transaction so two concurrent confirms can't both flip
    status — only one wins, the second returns bad_status.
    """
    db = _get_db()
    if db is None:
        return {"ok": False, "error": "store_unavailable"}

    ref = db.collection(COLLECTION).document(pending_id)

    # Outcome from inside the transaction. Reassigned via closure.
    outcome: dict = {"ok": False, "error": "unknown"}

    try:
        from firebase_admin import firestore as _fs
        transaction = db.transaction()

        @_fs.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            if not snap.exists:
                outcome.clear(); outcome["ok"] = False; outcome["error"] = "not_found"
                return
            pending = snap.to_dict() or {}
            if pending.get("status") != "pending":
                outcome.clear(); outcome["ok"] = False
                outcome["error"] = f"bad_status: {pending.get('status')}"
                return
            if pending.get("nonce") != nonce:
                outcome.clear(); outcome["ok"] = False; outcome["error"] = "bad_nonce"
                return
            if pending.get("userId") != user_id:
                outcome.clear(); outcome["ok"] = False; outcome["error"] = "wrong_user"
                return
            expires_at = pending.get("expiresAt")
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at) < _now():
                        txn.update(ref, {"status": "expired"})
                        outcome.clear(); outcome["ok"] = False; outcome["error"] = "expired"
                        return
                except Exception:
                    pass
            now_iso = _now_iso()
            txn.update(ref, {
                "status": "confirmed",
                "confirmedAt": now_iso,
                "confirmedBy": user_id,
            })
            pending["status"] = "confirmed"
            pending["confirmedAt"] = now_iso
            pending["confirmedBy"] = user_id
            outcome.clear(); outcome["ok"] = True; outcome["pending"] = pending

        _txn(transaction)
    except Exception as exc:
        return {"ok": False, "error": f"txn_failed: {exc}"}

    if not outcome.get("ok"):
        if outcome.get("error") in ("bad_nonce", "wrong_user"):
            logger.warning(
                "confirm rejected: id=%s err=%s user=%s",
                pending_id, outcome.get("error"), user_id,
            )
    else:
        logger.info("Pending action confirmed: id=%s by user=%s", pending_id, user_id)
    return outcome


def cancel(pending_id: str, *, user_id: str) -> dict:
    db = _get_db()
    if db is None:
        return {"ok": False, "error": "store_unavailable"}
    ref = db.collection(COLLECTION).document(pending_id)
    snap = ref.get()
    if not snap.exists:
        return {"ok": False, "error": "not_found"}
    pending = snap.to_dict() or {}
    if pending.get("userId") != user_id:
        return {"ok": False, "error": "wrong_user"}
    if pending.get("status") not in ("pending", "confirmed"):
        return {"ok": False, "error": f"bad_status: {pending.get('status')}"}
    ref.update({"status": "cancelled", "cancelledAt": _now_iso()})
    return {"ok": True}


def mark_executing(pending_id: str) -> bool:
    """Resume contract — call from the orchestrator BEFORE re-running the
    confirmed command. The caller MUST first verify that:
      (1) the pending doc's status is "confirmed",
      (2) the doc's userId matches the user driving the chat turn,
      (3) the doc's argsHash matches a fresh hash of the command about to run
          (`_hash_args(skill, command)`). If the model has changed args since
          confirmation, refuse — re-confirm with the new args.
    Then call mark_executing(); on success, run the command; then call
    mark_completed() with the result. This is enforced by the resume code
    path in main.py, NOT by mark_executing itself.
    """
    db = _get_db()
    if db is None:
        return False
    try:
        db.collection(COLLECTION).document(pending_id).update({
            "status": "executing",
            "executedAt": _now_iso(),
        })
        return True
    except Exception:
        return False


def mark_completed(pending_id: str, result: dict | None = None) -> bool:
    db = _get_db()
    if db is None:
        return False
    try:
        update: dict = {"status": "completed", "completedAt": _now_iso()}
        if result is not None:
            # Only stash a small summary — never the full data payload.
            summary = result.get("summary") if isinstance(result, dict) else None
            update["result"] = {"summary": summary, "status": result.get("status")} if summary else None
        db.collection(COLLECTION).document(pending_id).update(update)
        return True
    except Exception:
        return False


_LIST_REDACT_FIELDS = {"nonce"}


def list_pending_for_user(org_id: str, user_id: str, limit: int = 20) -> list[dict]:
    """Used by the frontend re-entry surface ('you have N pending actions').

    Strips `nonce` from the returned docs — the nonce is the auth secret the
    client uses to confirm. The client already has it from the original
    awaiting_confirmation tool envelope; the list endpoint returning it would
    let any caller with a valid token enumerate confirmation tokens for any
    user_id they pass.
    """
    db = _get_db()
    if db is None:
        return []
    try:
        snap = (
            db.collection(COLLECTION)
            .where("orgId", "==", org_id)
            .where("userId", "==", user_id)
            .where("status", "in", ["pending", "confirmed"])
            .order_by("createdAt", direction="DESCENDING")
            .limit(limit)
            .get()
        )
        out = []
        for d in snap:
            doc = d.to_dict() or {}
            for k in _LIST_REDACT_FIELDS:
                doc.pop(k, None)
            out.append(doc)
        return out
    except Exception as exc:
        logger.warning("list_pending_for_user failed: %s", exc)
        return []
