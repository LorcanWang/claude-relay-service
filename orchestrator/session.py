"""
Session store: Redis with in-memory fallback.

Key: orchestrator:session:{orgId}_{userId}
TTL: 24h (configurable via SESSION_TTL_SECONDS env)
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", str(24 * 3600)))
REDIS_PREFIX = "orchestrator:session:"

# ── in-memory fallback ────────────────────────────────────────────────────────
_memory_store: dict[str, dict] = {}


def _make_key(session_id: str) -> str:
    return f"{REDIS_PREFIX}{session_id}"


def _get_redis():
    """Return a redis.Redis client or None if unavailable."""
    try:
        import redis as _redis

        host = os.environ.get("REDIS_HOST", "127.0.0.1")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        password = os.environ.get("REDIS_PASSWORD") or None
        db = int(os.environ.get("REDIS_DB", "0"))
        client = _redis.Redis(host=host, port=port, password=password, db=db, socket_timeout=2)
        client.ping()
        return client
    except Exception as exc:
        logger.warning("Redis unavailable, using in-memory session store: %s", exc)
        return None


_redis_client = None
_redis_checked = False


def _redis():
    global _redis_client, _redis_checked
    if not _redis_checked:
        _redis_client = _get_redis()
        _redis_checked = True
    return _redis_client


# ── public API ────────────────────────────────────────────────────────────────

def get_session(session_id: str) -> Optional[dict]:
    key = _make_key(session_id)
    r = _redis()
    if r:
        try:
            raw = r.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning("Redis get failed: %s", exc)

    # Fallback to memory
    entry = _memory_store.get(key)
    if entry and time.time() < entry.get("_expires_at", 0):
        return entry["data"]
    if entry:
        _memory_store.pop(key, None)
    return None


def save_session(session_id: str, data: dict) -> None:
    data["last_active"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    key = _make_key(session_id)
    r = _redis()
    if r:
        try:
            r.setex(key, SESSION_TTL, json.dumps(data, default=str))
            return
        except Exception as exc:
            logger.warning("Redis set failed: %s", exc)

    # Fallback to memory
    _memory_store[key] = {
        "data": data,
        "_expires_at": time.time() + SESSION_TTL,
    }


def clear_session(session_id: str) -> None:
    key = _make_key(session_id)
    r = _redis()
    if r:
        try:
            r.delete(key)
        except Exception:
            pass
    _memory_store.pop(key, None)


def new_session(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "messages": [],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_active": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": SESSION_TTL,
    }
