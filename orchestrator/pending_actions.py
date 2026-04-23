"""
Pending action store — Firestore-backed confirmation gate for actions
declared with `requiresConfirmation: true` in their skill manifest.

Every requires-confirmation tool call writes a doc here BEFORE executing.
The frontend renders a confirm/cancel UI; on approve, it POSTs to the
orchestrator's /pending-actions/{id}/confirm endpoint with the per-user
nonce. The orchestrator marks status=confirmed; the next chat turn
checks for confirmed-but-unexecuted entries (atomically, via
claim_confirmed_for_execution) and resumes.

Design (from the 10-round Codex review, R7 verdict):
  - Server-side authority — never trust client-side `confirmed: true` alone.
  - Per-user nonce — only the requesting user can approve.
  - Args hash binding — if the model changes args, the nonce becomes invalid.
  - State machine: PENDING → CONFIRMED → EXECUTING → COMPLETED
                                      ↘ CANCELLED
                       PENDING → EXPIRED (background sweeper / lazy)

Trust boundary note: the nonce is delivered to the requesting user inside the
`awaiting_confirmation` tool envelope, which means it traverses the Anthropic
API as part of the assistant message stream. We accept this trust assumption
(Anthropic is a non-adversarial API provider). The userId binding remains the
real defense: even if the nonce leaked, only the original requester (verified
server-side via Firebase Auth) can confirm.
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


def _load_room_supervisors(db, room_id: str) -> list[str]:
    """Read chatRoom.supervisorUserIds. Falls back to [createdBy] if missing,
    or [] if the room itself doesn't exist (no approval is possible)."""
    if not room_id:
        return []
    try:
        snap = db.collection("chatRooms").document(room_id).get()
    except Exception as exc:
        logger.warning("Failed to load room %s supervisors: %s", room_id, exc)
        return []
    if not snap.exists:
        return []
    data = snap.to_dict() or {}
    supers = data.get("supervisorUserIds") or []
    if not supers:
        creator = data.get("createdBy")
        if creator:
            return [creator]
    return [s for s in supers if s]


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
    skill_configs: dict | None = None,
    in_platform: bool = True,
    long_running: bool = False,
    is_gap: bool = False,
) -> dict | None:
    """Create a pending action doc. SNAPSHOTS the room's supervisorUserIds
    onto the doc at create time so later supervisor edits don't retroactively
    affect already-pending actions.

    Returns the created record (with id) or None if Firestore is unavailable
    OR the room has no supervisors assigned.
    """
    db = _get_db()
    if db is None:
        logger.warning("Firestore unavailable — cannot create pending action")
        return None

    supervisors = _load_room_supervisors(db, room_id or "")
    if not supervisors:
        # Fail safe: no supervisors → no approval possible → don't create the
        # pending. Caller's tool envelope will signal an error.
        logger.warning(
            "Refusing to create pending — room %s has no supervisors", room_id,
        )
        return None

    pending_id = uuid.uuid4().hex
    nonce = secrets.token_urlsafe(24)  # legacy field, no longer enforced
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
        # Snapshot of who can approve this specific action. Frozen at create
        # time — supervisor edits to the room don't retroactively affect this.
        "roomSupervisorUserIds": supervisors,
        # Snapshot of skill configs at create time so server-side execution
        # after supervisor approval runs against the SAME account/context
        # the requester intended (not a default that could drift). Without
        # this snapshot, a Google Ads action approved 20 min after creation
        # could run against a different campaign than the user reviewed.
        "skillConfigsSnapshot": skill_configs or {},
        "inPlatformSnapshot": bool(in_platform),
        # Phase 7: if the approved action opts into durable execution, the
        # /confirm endpoint spawns a task instead of running synchronously.
        # Stored at create time so /confirm doesn't need to re-match the
        # action against the manifest.
        "longRunning": bool(long_running),
        # Phase 9: gap pendings are created when strict mode blocks an
        # undeclared skill action. Supervisor approval path is identical to
        # regular pendings; the flag is carried so the UI can render a
        # distinct "undeclared action" header and the /hive/signoff list can
        # highlight them for manifest-promotion review.
        "isGap": bool(is_gap),
        "nonce": nonce,
        "status": "pending",
        "createdAt": now.isoformat(),
        "expiresAt": expires.isoformat(),
        "confirmedAt": None,
        "confirmedBy": None,
        "executedAt": None,
        "result": None,
        # Populated by /confirm when the approval spawns a task — lets the UI
        # redirect the pending card's "still running?" state to the task card.
        "taskId": None,
    }
    try:
        db.collection(COLLECTION).document(pending_id).set(doc)
        logger.info(
            "Pending action created: id=%s skill=%s action=%s requester=%s supervisors=%s",
            pending_id, skill, action_id, user_id, supervisors,
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


def confirm(pending_id: str, *, user_id: str) -> dict:
    """Mark a pending action CONFIRMED. Authority rules:

      1. caller must be in `pending.roomSupervisorUserIds` (snapshot)
      2. status must still be "pending" and not expired

    Self-approval is allowed — supervisor membership is the policy. If
    you want segregation of duties, assign a different supervisor in
    the room settings.

    Transactional so concurrent confirms can't both succeed. Nonce is
    no longer enforced (legacy field) since chat-side approval buttons
    were removed; supervisor membership + Firebase Auth is the model.

    Returns {ok: bool, error?: str, pending?: dict}.
    """
    db = _get_db()
    if db is None:
        return {"ok": False, "error": "store_unavailable"}

    ref = db.collection(COLLECTION).document(pending_id)
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

            # (1) Supervisor membership.
            supers = pending.get("roomSupervisorUserIds") or []
            if user_id not in supers:
                outcome.clear(); outcome["ok"] = False
                outcome["error"] = "not_supervisor"
                return

            # (2) Freshness.
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
        if outcome.get("error") == "not_supervisor":
            logger.warning(
                "confirm rejected: id=%s err=%s user=%s",
                pending_id, outcome.get("error"), user_id,
            )
    else:
        logger.info("Pending action confirmed: id=%s by user=%s", pending_id, user_id)
    return outcome


def cancel(pending_id: str, *, user_id: str) -> dict:
    """Cancel a pending or confirmed action. Authority — caller must be EITHER:
      - the original requester (cancel your own request before approval), OR
      - a supervisor of the room (kill anything that shouldn't run)

    Transactional so it can't race with claim_confirmed_for_execution — if
    resume already flipped to "executing", cancel returns bad_status.
    """
    db = _get_db()
    if db is None:
        return {"ok": False, "error": "store_unavailable"}

    ref = db.collection(COLLECTION).document(pending_id)
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
            is_requester = pending.get("userId") == user_id
            supers = pending.get("roomSupervisorUserIds") or []
            is_supervisor = user_id in supers
            if not (is_requester or is_supervisor):
                outcome.clear(); outcome["ok"] = False; outcome["error"] = "not_authorized"
                return
            if pending.get("status") not in ("pending", "confirmed"):
                outcome.clear(); outcome["ok"] = False
                outcome["error"] = f"bad_status: {pending.get('status')}"
                return
            txn.update(ref, {
                "status": "cancelled",
                "cancelledAt": _now_iso(),
                "cancelledBy": user_id,
                "cancelledByRole": "supervisor" if is_supervisor else "requester",
            })
            outcome.clear(); outcome["ok"] = True

        _txn(transaction)
    except Exception as exc:
        return {"ok": False, "error": f"txn_failed: {exc}"}
    return outcome


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
    """Flip executing → completed. Transactional so a late completion can't
    overwrite a sweeper-flipped `failed` doc — the executing precondition
    check inside the txn means an already-failed doc just stays failed.
    Returns True only when we actually flipped, False on no-op or error."""
    db = _get_db()
    if db is None:
        return False
    ref = db.collection(COLLECTION).document(pending_id)
    summary = result.get("summary") if isinstance(result, dict) else None
    if summary:
        result_payload = {"summary": summary, "status": result.get("status")}
        # Carry through the error excerpt when the caller persisted one so
        # the admin UI / debug paths can show it without re-querying logs.
        err = result.get("error") if isinstance(result, dict) else None
        if isinstance(err, str) and err.strip():
            result_payload["error"] = err.strip()[:500]
    else:
        result_payload = None
    try:
        from firebase_admin import firestore as _fs
        transaction = db.transaction()

        @_fs.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            doc = snap.to_dict() or {}
            if doc.get("status") != "executing":
                # Already cancelled / failed (sweeper) / completed (race) —
                # don't clobber.
                logger.info(
                    "mark_completed no-op: id=%s already status=%s",
                    pending_id, doc.get("status"),
                )
                return False
            update = {"status": "completed", "completedAt": _now_iso()}
            if result_payload is not None:
                update["result"] = result_payload
            txn.update(ref, update)
            return True

        return bool(_txn(transaction))
    except Exception as exc:
        logger.warning("mark_completed txn failed for %s: %s", pending_id, exc)
        return False


_LIST_REDACT_FIELDS = {"nonce"}


def claim_confirmed_for_execution(
    org_id: str,
    user_id: str,
    skill: str,
    command: str,
) -> dict | None:
    """Atomically find AND claim a confirmed pending action for execution.

    Used by the dispatcher to detect "the user already approved this exact
    command" AND flip the chosen doc from confirmed → executing in one
    transaction. Two concurrent resume turns can't both win because the
    transactional update is conditional on `status=="confirmed"` at commit.

    Match criteria (same as the previous find-only version):
      - org/user binding (only the requester's confirmation counts)
      - argsHash exact match (model reformulation → no resume, gate again)
      - status == "confirmed" at the moment we claim it
      - not expired

    Returns the claimed doc (with id, status now "executing") or None if
    no eligible pending exists / another caller raced us.

    Caller MUST follow with `mark_completed()` after running, success or
    failure, so the audit log captures the outcome.
    """
    db = _get_db()
    if db is None:
        return None
    target_hash = _hash_args(skill, command)
    try:
        # Query candidates outside the transaction to keep the txn small.
        snap = (
            db.collection(COLLECTION)
            .where("orgId", "==", org_id)
            .where("userId", "==", user_id)
            .where("argsHash", "==", target_hash)
            .where("status", "==", "confirmed")
            .order_by("createdAt", direction="DESCENDING")
            .limit(5)
            .get()
        )
    except Exception as exc:
        logger.warning("claim_confirmed: query failed: %s", exc)
        return None

    now = _now()
    candidates: list[str] = []
    for d in snap:
        doc = d.to_dict() or {}
        try:
            if datetime.fromisoformat(doc.get("expiresAt", "")) < now:
                continue
        except Exception:
            continue
        if doc.get("id"):
            candidates.append(doc["id"])

    # Try to atomically claim each candidate in turn until one succeeds.
    from firebase_admin import firestore as _fs

    for pending_id in candidates:
        ref = db.collection(COLLECTION).document(pending_id)
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
                # Re-verify under the lock — another caller may have claimed,
                # cancelled, or expired the doc since we listed it.
                if doc2.get("status") != "confirmed":
                    outcome.clear(); outcome["ok"] = False
                    return
                if doc2.get("argsHash") != target_hash:
                    outcome.clear(); outcome["ok"] = False
                    return
                if doc2.get("userId") != user_id:
                    outcome.clear(); outcome["ok"] = False
                    return
                try:
                    if datetime.fromisoformat(doc2.get("expiresAt", "")) < _now():
                        txn.update(ref, {"status": "expired"})
                        outcome.clear(); outcome["ok"] = False
                        return
                except Exception:
                    pass
                txn.update(ref, {
                    "status": "executing",
                    "executedAt": _now_iso(),
                })
                doc2["status"] = "executing"
                outcome.clear(); outcome["ok"] = True; outcome["doc"] = doc2

            _txn(transaction)
        except Exception as exc:
            logger.warning("claim_confirmed: txn failed for %s: %s", pending_id, exc)
            continue

        if outcome.get("ok"):
            logger.info(
                "Claimed confirmed pending for execution: id=%s skill=%s",
                pending_id, skill,
            )
            return outcome["doc"]
    return None


def claim_specific_for_execution(pending_id: str) -> dict | None:
    """Atomically flip a specific pending from confirmed → executing.
    Returns the claimed doc on success, None if the doc isn't in
    "confirmed" state (already executing, completed, cancelled, or
    expired). Caller MUST run the command and call mark_completed.

    Used by the /hive/signoff approve handler to execute the action
    server-side in the same request as the confirmation. Different from
    claim_confirmed_for_execution (chat resume path), which searches by
    skill+command rather than id.
    """
    db = _get_db()
    if db is None:
        return None
    ref = db.collection(COLLECTION).document(pending_id)
    outcome: dict = {"ok": False}
    try:
        from firebase_admin import firestore as _fs
        transaction = db.transaction()

        @_fs.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            if not snap.exists:
                outcome.clear(); outcome["ok"] = False
                return
            doc = snap.to_dict() or {}
            if doc.get("status") != "confirmed":
                outcome.clear(); outcome["ok"] = False
                return
            try:
                if datetime.fromisoformat(doc.get("expiresAt", "")) < _now():
                    txn.update(ref, {"status": "expired"})
                    outcome.clear(); outcome["ok"] = False
                    return
            except Exception:
                pass
            txn.update(ref, {
                "status": "executing",
                "executedAt": _now_iso(),
            })
            doc["status"] = "executing"
            outcome.clear(); outcome["ok"] = True; outcome["doc"] = doc

        _txn(transaction)
    except Exception as exc:
        logger.warning("claim_specific: txn failed for %s: %s", pending_id, exc)
        return None
    if outcome.get("ok"):
        return outcome["doc"]
    return None


def find_confirmed_for_resume(
    org_id: str,
    user_id: str,
    skill: str,
    command: str,
) -> dict | None:
    """DEPRECATED — kept for backward compat. Use claim_confirmed_for_execution
    instead, which atomically flips the status to "executing" so two concurrent
    resumes can't both run.
    """
    db = _get_db()
    if db is None:
        return None
    target_hash = _hash_args(skill, command)
    try:
        snap = (
            db.collection(COLLECTION)
            .where("orgId", "==", org_id)
            .where("userId", "==", user_id)
            .where("argsHash", "==", target_hash)
            .where("status", "==", "confirmed")
            .order_by("createdAt", direction="DESCENDING")
            .limit(5)
            .get()
        )
    except Exception as exc:
        logger.warning("find_confirmed_for_resume query failed: %s", exc)
        return None
    now = _now()
    for d in snap:
        doc = d.to_dict() or {}
        try:
            if datetime.fromisoformat(doc.get("expiresAt", "")) < now:
                continue
        except Exception:
            continue
        return doc
    return None


def _strip_redacted(doc: dict) -> dict:
    out = dict(doc)
    for k in _LIST_REDACT_FIELDS:
        out.pop(k, None)
    return out


def list_pending_for_requester(org_id: str, user_id: str, limit: int = 20) -> list[dict]:
    """Pending actions THIS USER requested (their own outbox / read-only history).
    Strips the legacy `nonce` field before returning.
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
        return [_strip_redacted(d.to_dict() or {}) for d in snap]
    except Exception as exc:
        logger.warning("list_pending_for_requester failed: %s", exc)
        return []


def list_pending_for_supervisor(org_id: str, user_id: str, limit: int = 50) -> list[dict]:
    """Pending actions awaiting THIS USER's approval — i.e. the user is in
    `roomSupervisorUserIds` of pending actions whose status is "pending".
    Used by the /hive/signoff dashboard.
    """
    db = _get_db()
    if db is None:
        return []
    try:
        snap = (
            db.collection(COLLECTION)
            .where("orgId", "==", org_id)
            .where("status", "==", "pending")
            .where("roomSupervisorUserIds", "array_contains", user_id)
            .order_by("createdAt", direction="DESCENDING")
            .limit(limit)
            .get()
        )
        return [_strip_redacted(d.to_dict() or {}) for d in snap]
    except Exception as exc:
        logger.warning("list_pending_for_supervisor failed: %s", exc)
        return []


# Backward-compat alias — older imports may still reference list_pending_for_user.
list_pending_for_user = list_pending_for_requester


# ── Post-approval side effects ───────────────────────────────────────────────
# When a supervisor acts from /hive/signoff (not chat), the original room has
# no signal that the action resolved — the chat sits frozen at
# "Queued — awaiting your confirmation". These helpers write:
#   (1) a synthetic assistant message into chatRooms/{roomId}/messages so the
#       live Firestore listener re-renders the chat with the outcome, and
#   (2) a Hermes memoryType="approval_decision" record for the audit trail
#       surfaced in /admin/hermes-insights.


def _resolve_display_name(db, uid: str) -> str:
    """Look up users/{uid}.displayName for chat attribution. Falls back to
    email or a short uid prefix so the message is never blank."""
    if not uid:
        return "a supervisor"
    try:
        snap = db.collection("users").document(uid).get()
        if snap.exists:
            data = snap.to_dict() or {}
            return data.get("displayName") or data.get("email") or uid[:8]
    except Exception:
        pass
    return uid[:8]


def _format_outcome_text(
    outcome: str,
    title: str,
    actor_name: str,
    error_excerpt: str | None = None,
) -> str:
    if outcome == "approved_executed":
        return f"**Approved by {actor_name}** — {title} ran successfully."
    if outcome == "approved_failed":
        # Include the actual error so the user (and Lynx in the next turn) can
        # see what failed. Keep it short — a long stderr dump in chat is noisy.
        if error_excerpt:
            snippet = error_excerpt.strip()[:300]
            return (
                f"**Approved by {actor_name}** — {title} ran but errored:\n"
                f"```\n{snippet}\n```"
            )
        return f"**Approved by {actor_name}** — {title} executed but reported an error."
    if outcome == "approved_revoked":
        return (
            f"**Approved by {actor_name}**, but {title} was skipped — "
            "the skill is no longer enabled in this room."
        )
    if outcome == "approved_task_started":
        return (
            f"**Approved by {actor_name}** — {title} is now running as a task. "
            "The result will appear here when it finishes."
        )
    if outcome == "cancelled":
        return f"**Cancelled by {actor_name}** — {title} will not run."
    if outcome == "expired":
        return (
            f"**Expired** — {title} was not approved in time (30-min TTL) "
            "and will not run."
        )
    return f"{title}: {outcome}"


def post_room_approval_message(
    *,
    pending: dict,
    outcome: str,
    actor_uid: str,
    error_excerpt: str | None = None,
    attachments: list[dict] | None = None,
) -> None:
    """Append a synthetic assistant message to the pending's chat room so the
    UI reflects the sign-off outcome. No-op if the pending has no roomId
    (direct-chat pendings; future scope).

    `error_excerpt` is an optional short string from the failed exec_result
    (typically the skill's `error` field or a stderr tail). When provided,
    it's included verbatim in the synthetic message so the user can see
    what went wrong without ssh'ing the orchestrator log.

    `attachments` is an optional list of `{attachmentId, name, mimeType, sizeBytes}`
    dicts that get embedded as `data-attachment` parts in the message so the
    chat UI renders previews / download chips alongside the approval text.

    Uses the shared transactional helper so concurrent writers (active chat
    stream + this approval outcome + future task-result posts) can't race on
    the `index` field.
    """
    db = _get_db()
    if db is None:
        return
    room_id = pending.get("roomId")
    if not room_id:
        return

    actor_name = _resolve_display_name(db, actor_uid)
    title = pending.get("actionTitle") or pending.get("actionId") or "action"
    text = _format_outcome_text(outcome, title, actor_name, error_excerpt)

    # Append public URLs to the text body so the next LLM turn can reference
    # generated assets (e.g. pass the image URL to instagram-post). The
    # data-attachment parts give the UI a preview chip, but ui_to_anthropic
    # drops non-text parts — the model only sees `content`. Without this,
    # the model hallucinates a URL from training data.
    if attachments:
        url_lines = []
        for att in attachments:
            url = att.get("url")
            if url:
                url_lines.append(f"- [{att.get('name', 'file')}]({url})")
        if url_lines:
            text += "\n\n**Generated files:**\n" + "\n".join(url_lines)

    from room_messages import post_synthetic_message
    post_synthetic_message(
        room_id=room_id,
        text=text,
        attachments=attachments,
        meta={
            "kind": "approval_outcome",
            "outcome": outcome,
            "pendingId": pending.get("id"),
            "actorUid": actor_uid,
            "actionId": pending.get("actionId"),
            "skill": pending.get("skill"),
        },
    )


def write_approval_memory(*, pending: dict, outcome: str, actor_uid: str) -> None:
    """Persist an approval_decision Hermes memory. Non-fatal on failure."""
    try:
        from hermes_store import write_memory
    except Exception as exc:
        logger.warning("write_approval_memory: hermes_store unavailable: %s", exc)
        return

    db = _get_db()
    actor_name = (
        _resolve_display_name(db, actor_uid) if db else (actor_uid or "supervisor")
    )
    requester_uid = pending.get("userId") or ""
    requester_name = _resolve_display_name(db, requester_uid) if db else ""

    org_id = pending.get("orgId") or ""
    room_id = pending.get("roomId") or ""
    title = pending.get("actionTitle") or pending.get("actionId") or "action"

    verb = {
        "approved_executed": "approved",
        "approved_failed": "approved (execution failed)",
        "approved_revoked": "approved but skipped (skill no longer enabled)",
        "approved_task_started": "approved (queued as long-running task)",
        "cancelled": "cancelled",
        "expired": "expired unapproved",
    }.get(outcome, outcome)

    summary_parts = [f"{actor_name} {verb} {title}."]
    if requester_name and requester_name != actor_name:
        summary_parts.append(f"Requested by {requester_name}.")
    if pending.get("affectsAdSpend"):
        summary_parts.append("Flagged: affects ad spend.")
    if pending.get("destructive"):
        summary_parts.append("Flagged: destructive.")

    importance = 50 if (pending.get("destructive") or pending.get("affectsAdSpend")) else 30

    memory = {
        "orgId": org_id,
        "scopeType": "room" if room_id else "org",
        "scopeId": room_id or org_id,
        "memoryType": "approval_decision",
        "title": f"{verb.capitalize()}: {title}",
        "summary": " ".join(summary_parts),
        "importance": importance,
        "confidence": 1.0,
        "relevanceTags": [
            t for t in ["approval", outcome, pending.get("skill") or ""] if t
        ],
        "actorRefs": [
            {"displayName": actor_name, "uid": actor_uid, "role": "approver"},
            *(
                [{"displayName": requester_name, "uid": requester_uid, "role": "requester"}]
                if requester_uid else []
            ),
        ],
        "extra": {
            "pendingId": pending.get("id"),
            "actionId": pending.get("actionId"),
            "skill": pending.get("skill"),
            "command": (pending.get("command") or "")[:500],
            "argsHash": pending.get("argsHash"),
            "outcome": outcome,
            "roomId": room_id or None,
            "affectsAdSpend": bool(pending.get("affectsAdSpend")),
            "destructive": bool(pending.get("destructive")),
        },
    }
    try:
        write_memory(memory)
    except Exception as exc:
        logger.warning("write_approval_memory: persistence failed: %s", exc)


def sweep_stuck_executing(stale_seconds: int = 600) -> int:
    """Find pending docs stuck in `executing` for longer than the stale
    threshold and flip them to `failed`.

    Background task that protects against orphans when the chat handler
    successfully claims a pending → starts the subprocess → the client
    disconnects mid-await. The to_thread wrapper at the call site uses
    asyncio.shield so the post-await mark_pending_completed runs in
    normal cases, but a process crash / OOM still leaves the doc
    executing. This sweeper is the second line of defense.

    Returns the number of docs flipped. Intended to be called on a
    timer (e.g. every 60s) from a startup background task.
    """
    db = _get_db()
    if db is None:
        return 0
    cutoff = (_now() - timedelta(seconds=stale_seconds)).isoformat()
    try:
        snap = (
            db.collection(COLLECTION)
            .where("status", "==", "executing")
            .where("executedAt", "<", cutoff)
            .limit(50)
            .get()
        )
    except Exception as exc:
        logger.warning("sweep_stuck_executing query failed: %s", exc)
        return 0

    flipped = 0
    for d in snap:
        try:
            d.reference.update({
                "status": "failed",
                "completedAt": _now_iso(),
                "result": {
                    "summary": "execution did not finish — sweeper recovered after stale timeout",
                    "status": "error",
                },
            })
            flipped += 1
            logger.warning(
                "sweep_stuck_executing: flipped pending %s (skill=%s) to failed",
                d.id, (d.to_dict() or {}).get("skill"),
            )
        except Exception as exc:
            logger.warning("sweep_stuck_executing: update failed for %s: %s", d.id, exc)
    return flipped


def load_pending(pending_id: str) -> dict | None:
    """Fetch the current pending doc by id. Used by /cancel to hydrate the
    document for post-effects (the transactional cancel() returns only ok/error)."""
    db = _get_db()
    if db is None:
        return None
    try:
        snap = db.collection(COLLECTION).document(pending_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data["id"] = pending_id
        return data
    except Exception as exc:
        logger.warning("load_pending failed (id=%s): %s", pending_id, exc)
        return None
