import asyncio
import logging
from collections import defaultdict


logger = logging.getLogger("orchestrator.status_hub")


class StatusHub:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[session_id].add(q)
        logger.debug(
            "Subscribed queue for session [%s], subscribers=%d",
            session_id,
            len(self._subscribers[session_id]),
        )
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue):
        subscribers = self._subscribers.get(session_id)
        if not subscribers:
            return
        subscribers.discard(q)
        if not subscribers:
            self._subscribers.pop(session_id, None)
        logger.debug(
            "Unsubscribed queue for session [%s], remaining=%d",
            session_id,
            len(self._subscribers.get(session_id, ())),
        )

    def publish(self, session_id: str, event):
        subscribers = self._subscribers.get(session_id)
        if not subscribers:
            return
        for q in tuple(subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
            except Exception:
                logger.exception("Failed publishing status event for session [%s]", session_id)


status_hub = StatusHub()
