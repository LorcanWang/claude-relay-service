#!/usr/bin/env python3
"""
Task Worker — background process that executes durable long-running skill
actions (those with `longRunning: true` in their manifest).

Why a separate process (not extending hermes_worker.py):
  Hermes worker's failure model is "drop on floor" — events are extraction
  hints, a missed one is fine. Task worker's failure model is "user-visible
  work, must not drop." Different reliability contracts → different process.

Main loop:
  1. Sweep stale `running` tasks (heartbeat older than HEARTBEAT_STALE_SECONDS
     means the previous worker died; mark them failed so the user sees the
     outcome instead of the task dangling forever).
  2. Atomically claim one queued task.
  3. Run it via execute_command with TASK_TIMEOUT_SECONDS (overrides the
     module-level SKILL_TIMEOUT so long-running skills get their full budget).
  4. Post the result back to the room's chat, mark completed/failed
     idempotently via resultMessageId.

Run:
  python task_worker.py

  Or as a launchd managed process — see com.claude-task-worker.plist.

Environment:
  TASK_HEARTBEAT_STALE_SECONDS   (default 300)  sweeper cutoff
  TASK_TIMEOUT_SECONDS           (default 3600) per-task wall clock ceiling
  TASK_SWEEP_INTERVAL_SECONDS    (default 45)   sweeper/listener-fallback cadence
  TASK_WORKER_POLL_SECONDS       (default 3)    idle poll interval
  GOOGLE_APPLICATION_CREDENTIALS                Firebase service account path
  SKILL_ROOT                                    same as orchestrator
"""
from __future__ import annotations

import logging
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

# Load .env in the same pattern as main.py / hermes_worker.py
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from executor import execute_command
from hermes_store import _get_db
from room_messages import post_synthetic_assistant_message
from tasks import (
    HEARTBEAT_STALE_SECONDS,
    SWEEP_INTERVAL_SECONDS,
    TASK_TIMEOUT_SECONDS,
    claim_queued_task,
    heartbeat,
    list_queued,
    mark_completed,
    mark_failed,
    record_result_message_id,
    sweep_stale_running,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("task_worker")

POLL_INTERVAL = int(os.environ.get("TASK_WORKER_POLL_SECONDS", "3"))
HEARTBEAT_INTERVAL = 30  # seconds — well under HEARTBEAT_STALE_SECONDS (300)


def _slugify_agent_id(name: str) -> str:
    """Duplicates the slug rule used by main.py. Inlined (not imported) so
    this worker stays independent of the HTTP server module.
    """
    slug = re.sub(r"\s+", "-", (name or "").strip().lower())
    slug = re.sub(r"[^a-z0-9_-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "specialist"


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _format_result_text(task: dict, ok: bool, summary: str, error: str = "") -> str:
    """Rendered into the room when the task finishes. Keep it short — the
    full result is in the task doc; the chat bubble is the surface."""
    title = task.get("actionTitle") or task.get("actionId") or "Task"
    if ok:
        if summary:
            return f"**{title}** finished.\n\n{summary}"
        return f"**{title}** finished successfully."
    # Failure branch
    if error:
        return f"**{title}** failed.\n\n{error[:500]}"
    return f"**{title}** failed."


def _check_still_enabled_in_room(task: dict) -> bool:
    """Re-verify at run time that the skill is still in the room's agentIds.
    If a supervisor approved a task at T0 but the room removed the skill at
    T1, we should NOT execute — matches the sync-approval revocation check
    in main.py's /confirm handler."""
    room_id = task.get("roomId")
    if not room_id:
        # No room scope (direct-chat tasks if we add them later) — no revocation.
        return True
    db = _get_db()
    if db is None:
        # Fail closed: if we can't verify, don't silently bypass the check.
        return False
    try:
        snap = db.collection("chatRooms").document(room_id).get()
        if not snap.exists:
            logger.warning("Revocation check: room %s missing", room_id)
            return False
        enabled = set((snap.to_dict() or {}).get("agentIds") or [])
        skill_slug = _slugify_agent_id(task.get("skill", ""))
        return skill_slug in enabled
    except Exception as exc:
        logger.warning("Revocation check failed (room=%s): %s", room_id, exc)
        return False


def _emit_tool_executed(task: dict, exec_result: dict) -> None:
    """Parity with the sync path in main.py — enqueue a Hermes `tool_executed`
    event so memory extraction captures the long-running tool call too. Non-
    fatal if Hermes is disabled / unavailable.
    """
    try:
        from hermes_emitter import emit_tool_executed
    except Exception:
        return
    try:
        ok = isinstance(exec_result, dict) and exec_result.get("ok") is not False
        summary = ""
        if isinstance(exec_result, dict):
            summary = str(
                exec_result.get("agentNote")
                or exec_result.get("error")
                or ""
            )
        emit_tool_executed(
            org_id=task.get("orgId") or "",
            session_id=task.get("sessionId") or "",
            tool_name="run_command",
            skill_name=task.get("skill") or "",
            result_ok=ok,
            result_summary=summary,
        )
    except Exception as exc:
        logger.debug("emit_tool_executed failed (non-fatal): %s", exc)


def _run_scheduled_turn(task: dict, worker_id: str, task_id: str) -> None:
    """Phase 11-redux: a scheduled run replays a natural-language prompt in a
    room's chat session. The cron route (Next.js) pre-built the full
    ChatRequest — this worker just POSTs it to the orchestrator's /chat
    endpoint and streams through to completion. All side effects (messages
    to the room, tool calls, confirmation gate, Hermes memory) happen
    inside the normal chat loop.

    We consume the SSE stream chunk-by-chunk but only inspect it to decide
    terminal status (completed / awaiting_confirmation / failed). The
    stream BODY is the source of truth for user-visible output — which
    already lands in chatRooms/{roomId}/messages via the chat loop.
    """
    import urllib.request
    import urllib.error

    body = task.get("scheduledTurnRequest")
    runner_url = task.get("runnerUrl")
    runner_key = task.get("runnerKey")
    if not body or not runner_url:
        mark_failed(
            task_id,
            error="scheduled_turn task missing scheduledTurnRequest or runnerUrl",
        )
        return

    # When task_worker runs on the same host as the orchestrator (the default
    # single-machine deployment), hit it via localhost to bypass Cloudflare —
    # the clawdbots.dev edge blocks Python's default urllib User-Agent with
    # error 1010 ("Access Denied"). Default is computed from the same
    # ORCHESTRATOR_PORT main.py reads (default 8090). An explicit
    # LOCAL_ORCHESTRATOR_URL env var overrides the default — set it to the
    # empty string to force the remote runner_url (e.g. task_worker on a
    # different host). runner_key auth works against localhost because the
    # /chat endpoint's verify_token dependency matches the same RUNNER_KEY
    # either way.
    local_url_env = os.environ.get("LOCAL_ORCHESTRATOR_URL")
    if local_url_env is None:
        orch_port = os.environ.get("ORCHESTRATOR_PORT", "8090")
        effective_base = f"http://localhost:{orch_port}"
    else:
        effective_base = local_url_env.strip() or runner_url
    url = effective_base.rstrip("/") + "/chat"
    import json as _json
    req_body = _json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        # Set even when we go direct to localhost — cheap, and the next
        # operator who changes LOCAL_ORCHESTRATOR_URL to a Cloudflare-
        # fronted host won't be surprised by a 1010 rejection.
        "User-Agent": "lynx-task-worker/1.0 (+https://zeonsolutions.ai)",
    }
    if runner_key:
        headers["Authorization"] = f"Bearer {runner_key}"

    logger.info(
        "[schedule] posting scheduled turn: task=%s room=%s url=%s body_bytes=%d",
        task_id, body.get("roomId"), url, len(req_body),
    )

    req = urllib.request.Request(url, data=req_body, headers=headers, method="POST")

    # Track terminal status inferred from the stream — the chat loop emits
    # a `data-action` with action="pending" when it short-circuits on a
    # confirmation gate; we surface that as awaiting_confirmation on the
    # schedule's lastRunStatus.
    saw_pending = False
    last_error: str | None = None
    started = time.time()
    final_summary = ""
    try:
        with urllib.request.urlopen(req, timeout=max(60, TASK_TIMEOUT_SECONDS)) as resp:
            status_code = resp.getcode()
            if status_code != 200:
                raise RuntimeError(f"orchestrator /chat returned HTTP {status_code}")
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = _json.loads(payload)
                except Exception:
                    continue
                etype = ev.get("type") if isinstance(ev, dict) else None
                if etype == "error":
                    last_error = str(ev.get("error") or "chat-loop error")
                elif etype == "data-action":
                    data = ev.get("data") if isinstance(ev, dict) else None
                    if isinstance(data, dict) and data.get("action") == "pending":
                        saw_pending = True
                elif etype == "text-delta":
                    delta = ev.get("delta") if isinstance(ev, dict) else None
                    if isinstance(delta, str):
                        final_summary += delta
    except urllib.error.HTTPError as exc:
        try:
            body_bytes = exc.read() if exc else b""
        except Exception:
            body_bytes = b""
        mark_failed(
            task_id,
            error=f"HTTP {exc.code}: {body_bytes.decode('utf-8', errors='replace')[:400]}",
        )
        return
    except Exception as exc:
        mark_failed(task_id, error=f"scheduled_turn stream failed: {exc}")
        return

    elapsed = time.time() - started
    logger.info(
        "[schedule] scheduled turn done: task=%s elapsed=%.1fs pending=%s err=%s",
        task_id, elapsed, saw_pending, bool(last_error),
    )

    # Persist the turn to chatRooms/{roomId}/messages. The orchestrator's
    # /chat endpoint only writes to the Redis session (for the agent loop);
    # it's the browser client that normally writes to Firestore after the
    # SSE stream ends. For scheduled runs, task_worker IS the client —
    # so we persist here. Without this, the room's chat shows nothing for
    # scheduled turns even though the session + memory were updated.
    from room_messages import post_synthetic_message
    room_id_for_post = task.get("roomId")
    user_msg_id: str | None = None
    assistant_msg_id: str | None = None
    if room_id_for_post:
        # 1. The user-role turn — carries the [Scheduled run: …] prefix +
        #    the prompt so the room has visible context for what triggered
        #    the assistant reply. Attributed to the schedule's sender
        #    identity so the bubble doesn't look like a human asked.
        try:
            user_text = ""
            msgs = body.get("messages") if isinstance(body, dict) else None
            if isinstance(msgs, list) and msgs:
                parts = msgs[-1].get("parts") if isinstance(msgs[-1], dict) else None
                if isinstance(parts, list):
                    user_text = "".join(
                        (p.get("text") or "") for p in parts
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
            if user_text:
                user_msg_id = post_synthetic_message(
                    room_id=room_id_for_post,
                    text=user_text,
                    role="user",
                    sender_user_id=body.get("senderUserId"),
                    sender_display_name=body.get("senderDisplayName"),
                    meta={
                        "kind": "scheduled_turn_prompt",
                        "taskId": task_id,
                        "scheduleId": task.get("scheduleId"),
                    },
                )
        except Exception as exc:
            logger.warning(
                "[schedule] failed to post scheduled user message (task=%s): %s",
                task_id, exc,
            )

        # 2. The assistant answer — assembled from text-delta events during
        #    the stream. Intermediate tool calls don't get their own UI
        #    message today (the room just sees the final answer).
        if final_summary.strip():
            try:
                assistant_msg_id = post_synthetic_message(
                    room_id=room_id_for_post,
                    text=final_summary,
                    role="assistant",
                    meta={
                        "kind": "scheduled_turn_result",
                        "taskId": task_id,
                        "scheduleId": task.get("scheduleId"),
                        "awaitingConfirmation": saw_pending,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "[schedule] failed to post scheduled assistant message (task=%s): %s",
                    task_id, exc,
                )

    # Reflect terminal status back onto the schedule doc so the UI badge
    # updates without needing a secondary fetch.
    schedule_id = task.get("scheduleId")
    if schedule_id:
        _update_schedule_status(
            schedule_id,
            pending_id=None,  # Pending id would come from the chat loop; skip in MVP
            status=(
                "awaiting_confirmation" if saw_pending
                else ("failed" if last_error else "completed")
            ),
            last_task_id=task_id,
        )

    if last_error:
        mark_failed(task_id, error=last_error, result_message_id=assistant_msg_id)
    else:
        mark_completed(
            task_id,
            result={
                "ok": True,
                "kind": "scheduled_turn",
                "elapsedSeconds": round(elapsed, 1),
                "summary": final_summary[:500],
                "awaitingConfirmation": saw_pending,
                "userMessageId": user_msg_id,
            },
            result_message_id=assistant_msg_id,
        )


def _update_schedule_status(
    schedule_id: str,
    *,
    pending_id: str | None,
    status: str,
    last_task_id: str | None,
) -> None:
    """Write lastRunStatus + lastTaskId + lastPendingId onto the schedule
    doc. Non-fatal on failure — the schedule's source of truth is the
    next cron claim anyway."""
    try:
        db = _get_db()
        if db is None:
            return
        update = {
            "lastRunStatus": status,
            "updatedAt": _now_iso(),
        }
        if last_task_id:
            update["lastTaskId"] = last_task_id
        if pending_id is not None:
            update["lastPendingId"] = pending_id
        db.collection("schedules").document(schedule_id).update(update)
    except Exception as exc:
        logger.warning("schedule status update failed (id=%s): %s", schedule_id, exc)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def run_task(task: dict, worker_id: str) -> None:
    """Execute one claimed task end-to-end. Heartbeats on a daemon thread,
    posts the result message, then records the message id and flips to
    completed/failed — all idempotently, so a worker crash partway through
    completion still converges when re-entered by the sweeper."""
    task_id = task.get("id") or ""
    if not task_id:
        logger.error("run_task: task missing id: %r", task)
        return

    # ── Heartbeat thread — keeps the sweeper from reaping us mid-run. ──────
    stop_heartbeat = threading.Event()

    def _beat_loop():
        # Immediate tick so a freshly-claimed task has a fresh heartbeat
        # before the first long subprocess call starts.
        heartbeat(task_id)
        while not stop_heartbeat.wait(HEARTBEAT_INTERVAL):
            heartbeat(task_id)

    beat_thread = threading.Thread(target=_beat_loop, daemon=True)
    beat_thread.start()

    # Phase 11-redux: scheduled turns replay a prompt through /chat. They
    # skip the tool-call sandbox path entirely — no execute_command, no
    # revocation check. The orchestrator's chat loop owns revocation.
    kind = task.get("kind")
    if kind == "scheduled_turn":
        try:
            _run_scheduled_turn(task, worker_id, task_id)
        finally:
            stop_heartbeat.set()
            beat_thread.join(timeout=2)
        return

    try:
        # ── Revocation check. ──────────────────────────────────────────────
        if not _check_still_enabled_in_room(task):
            logger.warning(
                "Task REVOKED: id=%s skill=%s room=%s — skill no longer enabled",
                task_id, task.get("skill"), task.get("roomId"),
            )
            text = (
                f"**{task.get('actionTitle') or task.get('actionId') or 'Task'}** "
                "was skipped — the skill is no longer enabled in this room."
            )
            msg_id = _post_result(task, text, kind="task_revoked")
            mark_failed(
                task_id,
                error="skill no longer enabled in room — execution refused",
                result_message_id=msg_id,
            )
            return

        # ── Execute. Use SNAPSHOT configs, not live request context. ──────
        exec_result = execute_command(
            task.get("skill") or "",
            task.get("command") or "",
            [task.get("skill") or ""],
            context={
                "org_id": task.get("orgId"),
                "user_id": task.get("userId"),
                "session_id": task.get("sessionId"),
                "in_platform": task.get("inPlatformSnapshot", True),
                "skill_configs": task.get("skillConfigsSnapshot") or {},
                "room_id": task.get("roomId"),
            },
            timeout_seconds=TASK_TIMEOUT_SECONDS,
        )
        exec_ok = isinstance(exec_result, dict) and exec_result.get("ok") is not False
        summary = ""
        if isinstance(exec_result, dict):
            summary = str(
                exec_result.get("agentNote")
                or (exec_result.get("data", {}) or {}).get("summary", "")
                if isinstance(exec_result.get("data"), dict)
                else exec_result.get("agentNote") or ""
            )
        error_msg = ""
        if not exec_ok and isinstance(exec_result, dict):
            error_msg = str(exec_result.get("error") or exec_result.get("stderr") or "")

        # ── Post the chat result FIRST, THEN transition status. ───────────
        # If the post fails we still transition but with no resultMessageId;
        # the sweeper won't re-process a completed/failed task. The user
        # loses the in-chat notification in that corner case — acceptable
        # since the task doc itself has the result.
        text = _format_result_text(task, exec_ok, summary, error_msg)
        msg_id = _post_result(
            task,
            text,
            kind="task_result",
            extra_meta={
                "outcome": "completed" if exec_ok else "failed",
            },
        )
        if exec_ok:
            mark_completed(task_id, result=exec_result, result_message_id=msg_id)
        else:
            mark_failed(task_id, error=error_msg or "unknown", result_message_id=msg_id)

        _emit_tool_executed(task, exec_result if isinstance(exec_result, dict) else {})

        logger.info(
            "Task done: id=%s skill=%s ok=%s worker=%s",
            task_id, task.get("skill"), exec_ok, worker_id,
        )
    except Exception as exc:
        logger.exception("Task crashed: id=%s", task_id)
        # Best-effort failure post + mark_failed. If the exception happened
        # before execute_command returned, we still want the user to see
        # that it died, not wait forever for a result.
        try:
            text = f"**{task.get('actionTitle') or 'Task'}** crashed: {exc}"
            msg_id = _post_result(task, text, kind="task_crashed")
            mark_failed(task_id, error=f"worker exception: {exc}", result_message_id=msg_id)
        except Exception:
            mark_failed(task_id, error=f"worker exception: {exc}")
    finally:
        stop_heartbeat.set()
        beat_thread.join(timeout=2)


def _post_result(
    task: dict,
    text: str,
    *,
    kind: str,
    extra_meta: Optional[dict] = None,
) -> Optional[str]:
    room_id = task.get("roomId")
    if not room_id:
        return None
    meta: dict = {
        "kind": kind,
        "taskId": task.get("id"),
        "actionId": task.get("actionId"),
        "skill": task.get("skill"),
    }
    if extra_meta:
        meta.update(extra_meta)
    msg_id = post_synthetic_assistant_message(
        room_id=room_id,
        text=text,
        meta=meta,
    )
    if msg_id and task.get("id"):
        # Record BEFORE marking completed so idempotency holds even if the
        # worker crashes between posting the message and the status flip.
        record_result_message_id(task["id"], msg_id)
    return msg_id


def main() -> None:
    worker_id = _worker_id()
    logger.info(
        "Task worker starting: id=%s skill_timeout=%ds heartbeat_stale=%ds poll=%ds sweep=%ds",
        worker_id, TASK_TIMEOUT_SECONDS, HEARTBEAT_STALE_SECONDS, POLL_INTERVAL, SWEEP_INTERVAL_SECONDS,
    )
    if _get_db() is None:
        logger.error("Firestore unavailable; worker cannot start. Set GOOGLE_APPLICATION_CREDENTIALS.")
        sys.exit(1)

    last_sweep = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_sweep >= SWEEP_INTERVAL_SECONDS:
                swept = sweep_stale_running()
                if swept:
                    logger.warning("Swept %d stale running task(s): %s", len(swept), swept)
                last_sweep = now

            task = claim_queued_task(worker_id=worker_id)
            if task:
                run_task(task, worker_id)
                # Loop back immediately — queue may have more.
                continue

            # Nothing to claim — sleep a short interval.
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Task worker shutting down on SIGINT")
            break
        except Exception as exc:
            logger.exception("Worker loop error: %s", exc)
            time.sleep(POLL_INTERVAL * 2)


if __name__ == "__main__":
    main()
