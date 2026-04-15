"""
Hermes Firestore store — read/write durable memory records.

Collections:
  hermesMemories   — canonical memory records (decisions, preferences, insights)
  hermesSessions   — durable session/room rollups
  hermesProfiles   — materialized user/customer profiles for prompt injection
"""

import logging
import os
import time
import uuid
from typing import Optional

logger = logging.getLogger("hermes.store")

_db = None
_init_attempted = False


def _get_db():
    global _db, _init_attempted
    if _init_attempted:
        return _db
    _init_attempted = True

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not firebase_admin._apps:
            if cred_path:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
            else:
                firebase_admin.initialize_app()
        _db = firestore.client()
        logger.info("Hermes Firestore connected")
    except Exception as exc:
        logger.warning("Firestore unavailable: %s", exc)
        _db = None
    return _db


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_memory(memory: dict) -> Optional[str]:
    db = _get_db()
    if not db:
        logger.warning("Firestore unavailable, memory not saved")
        return None

    if "id" not in memory:
        memory["id"] = str(uuid.uuid4())
    if "temporal" not in memory:
        memory["temporal"] = {}

    now = _now_iso()
    memory["temporal"].setdefault("observedAt", now)
    memory["temporal"].setdefault("firstSeenAt", now)
    memory["temporal"]["lastSeenAt"] = now
    memory.setdefault("status", "active")
    memory.setdefault("version", 1)
    memory.setdefault("retrieval", {"retrievalCount": 0, "pinned": False})

    try:
        db.collection("hermesMemories").document(memory["id"]).set(memory)
        logger.info(
            "Memory saved: %s type=%s org=%s",
            memory["id"], memory.get("memoryType"), memory.get("orgId"),
        )
        return memory["id"]
    except Exception as exc:
        logger.error("Failed to write memory: %s", exc)
        return None


def write_memories_batch(memories: list[dict]) -> list[str]:
    db = _get_db()
    if not db:
        return []

    now = _now_iso()
    batch = db.batch()
    ids = []

    for memory in memories:
        if "id" not in memory:
            memory["id"] = str(uuid.uuid4())
        if "temporal" not in memory:
            memory["temporal"] = {}
        memory["temporal"].setdefault("observedAt", now)
        memory["temporal"].setdefault("firstSeenAt", now)
        memory["temporal"]["lastSeenAt"] = now
        memory.setdefault("status", "active")
        memory.setdefault("version", 1)
        memory.setdefault("retrieval", {"retrievalCount": 0, "pinned": False})

        ref = db.collection("hermesMemories").document(memory["id"])
        batch.set(ref, memory)
        ids.append(memory["id"])

    try:
        batch.commit()
        logger.info("Batch wrote %d memories", len(ids))
    except Exception as exc:
        logger.error("Batch write failed: %s", exc)
        return []

    return ids


def upsert_session(session_id: str, data: dict):
    db = _get_db()
    if not db:
        return

    data["lastUpdated"] = _now_iso()
    try:
        db.collection("hermesSessions").document(session_id).set(data, merge=True)
    except Exception as exc:
        logger.error("Failed to upsert session: %s", exc)


def upsert_profile(profile_key: str, data: dict):
    db = _get_db()
    if not db:
        return

    data["lastUpdated"] = _now_iso()
    try:
        db.collection("hermesProfiles").document(profile_key).set(data, merge=True)
    except Exception as exc:
        logger.error("Failed to upsert profile: %s", exc)


def get_profile(profile_key: str) -> Optional[dict]:
    db = _get_db()
    if not db:
        return None

    try:
        doc = db.collection("hermesProfiles").document(profile_key).get()
        return doc.to_dict() if doc.exists else None
    except Exception as exc:
        logger.error("Failed to get profile: %s", exc)
        return None


def get_recent_memories(
    org_id: str,
    scope_type: Optional[str] = None,
    scope_id: Optional[str] = None,
    memory_types: Optional[list[str]] = None,
    limit: int = 20,
    min_importance: int = 0,
) -> list[dict]:
    db = _get_db()
    if not db:
        return []

    try:
        query = db.collection("hermesMemories").where("orgId", "==", org_id)

        if scope_type:
            query = query.where("scopeType", "==", scope_type)
        if scope_id:
            query = query.where("scopeId", "==", scope_id)
        if memory_types:
            query = query.where("memoryType", "in", memory_types)

        query = query.where("status", "==", "active")
        query = query.order_by("temporal.lastSeenAt", direction="DESCENDING")
        query = query.limit(limit)

        docs = query.stream()
        memories = []
        for doc in docs:
            m = doc.to_dict()
            if m.get("importance", 0) >= min_importance:
                memories.append(m)
        return memories
    except Exception as exc:
        logger.error("Failed to query memories: %s", exc)
        return []
