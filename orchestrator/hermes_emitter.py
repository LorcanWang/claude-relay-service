"""
Hermes event emitter — non-blocking event publishing for the orchestrator.

Emits lightweight events to a Redis list for the Hermes worker to consume.
Falls back to in-memory queue if Redis is unavailable.
"""

import json
import logging
import os
import time
import uuid
from typing import Optional

from hermes_classifier import classify_turn, TurnClassification

logger = logging.getLogger("hermes.emitter")

from hermes_redis import get_redis

HERMES_QUEUE_KEY = "hermes:queue:events"
HERMES_ENABLED = os.environ.get("HERMES_ENABLED", "true").lower() == "true"
BATCH_THRESHOLD = int(os.environ.get("HERMES_BATCH_THRESHOLD", "5"))

_memory_queue: list[dict] = []
_turn_buffer: dict[str, list[dict]] = {}


def _push_event(event: dict):
    event["id"] = str(uuid.uuid4())
    event["emitted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    r = get_redis()
    if r:
        try:
            r.rpush(HERMES_QUEUE_KEY, json.dumps(event, default=str))
            return
        except Exception as exc:
            logger.warning("Redis push failed: %s", exc)

    _memory_queue.append(event)
    if len(_memory_queue) > 1000:
        _memory_queue.pop(0)


def emit_turn_completed(
    org_id: str,
    user_id: str,
    session_id: str,
    room_id: Optional[str],
    sender_name: Optional[str],
    user_text: str,
    assistant_text: str,
    tool_names: list[str],
    message_index: int,
):
    if not HERMES_ENABLED:
        return

    user_cls = classify_turn(user_text, role="user", tool_names=tool_names)
    assistant_cls = classify_turn(assistant_text, role="assistant")

    combined_importance = max(user_cls.importance, assistant_cls.importance)

    if user_cls.is_trivial and assistant_cls.is_trivial:
        return

    turn_event = {
        "type": "chat_turn_completed",
        "org_id": org_id,
        "user_id": user_id,
        "session_id": session_id,
        "room_id": room_id,
        "sender_name": sender_name,
        "message_index": message_index,
        "user_text": user_text[:1000],
        "assistant_text": assistant_text[:2000],
        "tool_names": tool_names,
        "classification": {
            "user": user_cls.to_dict(),
            "assistant": assistant_cls.to_dict(),
        },
        "importance": combined_importance,
    }

    buffer_key = session_id
    if buffer_key not in _turn_buffer:
        _turn_buffer[buffer_key] = []
    _turn_buffer[buffer_key].append(turn_event)

    non_trivial_count = sum(
        1 for t in _turn_buffer[buffer_key]
        if t["importance"] >= 15
    )

    if non_trivial_count >= BATCH_THRESHOLD:
        _flush_buffer(buffer_key)
    else:
        high_importance = any(t["importance"] >= 50 for t in _turn_buffer[buffer_key])
        if high_importance:
            _flush_buffer(buffer_key)


def _flush_buffer(buffer_key: str):
    turns = _turn_buffer.pop(buffer_key, [])
    if not turns:
        return

    batch_event = {
        "type": "turn_batch_ready",
        "session_id": turns[0]["session_id"],
        "org_id": turns[0]["org_id"],
        "user_id": turns[0]["user_id"],
        "room_id": turns[0].get("room_id"),
        "turns": turns,
        "turn_count": len(turns),
    }

    _push_event(batch_event)
    logger.info(
        "Hermes batch flushed: session=%s turns=%d",
        buffer_key, len(turns),
    )


def emit_tool_executed(
    org_id: str,
    session_id: str,
    tool_name: str,
    skill_name: str,
    result_ok: bool,
    result_summary: str,
):
    if not HERMES_ENABLED:
        return

    _push_event({
        "type": "tool_executed",
        "org_id": org_id,
        "session_id": session_id,
        "tool_name": tool_name,
        "skill_name": skill_name,
        "result_ok": result_ok,
        "result_summary": result_summary[:500],
    })


def emit_session_closed(
    org_id: str,
    user_id: str,
    session_id: str,
    room_id: Optional[str],
    message_count: int,
    participant_names: Optional[list[str]] = None,
):
    if not HERMES_ENABLED:
        return

    _flush_buffer(session_id)

    _push_event({
        "type": "session_closed",
        "org_id": org_id,
        "user_id": user_id,
        "session_id": session_id,
        "room_id": room_id,
        "message_count": message_count,
        "participant_names": participant_names,
    })


def get_pending_events(max_count: int = 50) -> list[dict]:
    """Pop events from the queue. Used by hermes_worker."""
    r = get_redis()
    if r:
        try:
            events = []
            for _ in range(max_count):
                raw = r.lpop(HERMES_QUEUE_KEY)
                if raw is None:
                    break
                events.append(json.loads(raw))
            return events
        except Exception as exc:
            logger.warning("Redis pop failed: %s", exc)

    events = _memory_queue[:max_count]
    del _memory_queue[:max_count]
    return events


def get_queue_length() -> int:
    r = get_redis()
    if r:
        try:
            return r.llen(HERMES_QUEUE_KEY) or 0
        except Exception:
            pass
    return len(_memory_queue)
