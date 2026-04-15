"""
Shared Redis client for all Hermes modules.
Lazy-initialized, reuses a single connection.
"""

import logging
import os

logger = logging.getLogger("hermes.redis")

_client = None
_checked = False


def get_redis():
    global _client, _checked
    if _checked:
        return _client
    _checked = True

    try:
        import redis
        host = os.environ.get("REDIS_HOST", "127.0.0.1")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        password = os.environ.get("REDIS_PASSWORD") or None
        db = int(os.environ.get("REDIS_DB", "0"))
        _client = redis.Redis(
            host=host, port=port, password=password, db=db,
            socket_timeout=2, socket_connect_timeout=2,
        )
        _client.ping()
        logger.info("Hermes Redis connected: %s:%d", host, port)
    except Exception as exc:
        logger.warning("Hermes Redis unavailable: %s", exc)
        _client = None

    return _client
