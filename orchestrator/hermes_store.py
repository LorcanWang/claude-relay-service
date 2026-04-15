"""
Hermes Firestore store — read/write durable memory records.

Collections:
  hermesMemories   — canonical memory records (decisions, preferences, insights)
  hermesSessions   — durable session/room rollups
  hermesProfiles   — materialized user/customer profiles for prompt injection
"""

import hashlib
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


def _make_dedupe_key(memory: dict) -> str:
    """Deterministic dedupe key from org + scope + type + normalized title + summary fragment."""
    parts = [
        memory.get("orgId", ""),
        memory.get("scopeType", ""),
        memory.get("scopeId", ""),
        memory.get("memoryType", ""),
        memory.get("title", "").strip().lower(),
        memory.get("summary", "").strip().lower()[:80],
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _invalidate_retrieval_cache(org_id: str, scope_id: str):
    """Invalidate Redis retrieval cache when memories change."""
    try:
        from hermes_redis import get_redis
        r = get_redis()
        if not r:
            return
        pattern = f"hermes:cache:bundle:{org_id}:*"
        keys = r.keys(pattern)
        if keys:
            r.delete(*keys)
            logger.debug("Invalidated %d cache keys for org %s", len(keys), org_id)
    except Exception:
        pass


def _prepare_memory(memory: dict, now: str) -> dict:
    """Add standard fields and lifecycle metadata."""
    if "id" not in memory:
        memory["id"] = str(uuid.uuid4())
    if "temporal" not in memory:
        memory["temporal"] = {}

    memory["temporal"].setdefault("observedAt", now)
    memory["temporal"].setdefault("firstSeenAt", now)
    memory["temporal"]["lastSeenAt"] = now
    memory.setdefault("status", "active")
    memory.setdefault("version", 1)
    memory.setdefault("observationCount", 1)
    memory.setdefault("retrieval", {"retrievalCount": 0, "pinned": False})
    memory["dedupeKey"] = _make_dedupe_key(memory)
    return memory


def _find_existing(db, dedupe_key: str, org_id: str) -> Optional[dict]:
    """Find an existing memory with the same dedupe key."""
    try:
        docs = (
            db.collection("hermesMemories")
            .where("orgId", "==", org_id)
            .where("dedupeKey", "==", dedupe_key)
            .where("status", "==", "active")
            .limit(1)
            .stream()
        )
        for doc in docs:
            result = doc.to_dict()
            result["_doc_id"] = doc.id
            return result
    except Exception:
        pass
    return None


def write_memory(memory: dict) -> Optional[str]:
    db = _get_db()
    if not db:
        logger.warning("Firestore unavailable, memory not saved")
        return None

    now = _now_iso()
    memory = _prepare_memory(memory, now)

    existing = _find_existing(db, memory["dedupeKey"], memory.get("orgId", ""))
    if existing:
        doc_id = existing["_doc_id"]
        try:
            db.collection("hermesMemories").document(doc_id).update({
                "temporal.lastSeenAt": now,
                "observationCount": existing.get("observationCount", 1) + 1,
                "confidence": max(
                    existing.get("confidence", 0.5),
                    memory.get("confidence", 0.5),
                ),
                "summary": memory.get("summary", existing.get("summary", "")),
            })
            _invalidate_retrieval_cache(memory.get("orgId", ""), memory.get("scopeId", ""))
            logger.info("Memory deduplicated: %s (count=%d)", doc_id, existing.get("observationCount", 1) + 1)
            return doc_id
        except Exception as exc:
            logger.error("Failed to update existing memory: %s", exc)
            return None

    try:
        db.collection("hermesMemories").document(memory["id"]).set(memory)
        _invalidate_retrieval_cache(memory.get("orgId", ""), memory.get("scopeId", ""))
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
    ids = []
    new_count = 0
    org_ids_changed = set()

    for memory in memories:
        memory = _prepare_memory(memory, now)
        org_ids_changed.add(memory.get("orgId", ""))

        existing = _find_existing(db, memory["dedupeKey"], memory.get("orgId", ""))
        if existing:
            doc_id = existing["_doc_id"]
            try:
                db.collection("hermesMemories").document(doc_id).update({
                    "temporal.lastSeenAt": now,
                    "observationCount": existing.get("observationCount", 1) + 1,
                    "confidence": max(
                        existing.get("confidence", 0.5),
                        memory.get("confidence", 0.5),
                    ),
                    "summary": memory.get("summary", existing.get("summary", "")),
                })
                ids.append(doc_id)
                logger.debug("Deduped memory: %s", doc_id)
            except Exception as exc:
                logger.error("Failed to update memory: %s", exc)
            continue

        try:
            db.collection("hermesMemories").document(memory["id"]).set(memory)
            ids.append(memory["id"])
            new_count += 1
        except Exception as exc:
            logger.error("Failed to write memory: %s", exc)

    for org_id in org_ids_changed:
        _invalidate_retrieval_cache(org_id, "")

    logger.info("Batch processed %d memories (%d new)", len(ids), new_count)
    return ids


def supersede_memory(memory_id: str, new_memory: dict) -> Optional[str]:
    """Mark an old memory as superseded and create its replacement."""
    db = _get_db()
    if not db:
        return None

    now = _now_iso()
    try:
        db.collection("hermesMemories").document(memory_id).update({
            "status": "superseded",
            "temporal.supersededAt": now,
        })
    except Exception as exc:
        logger.error("Failed to supersede memory %s: %s", memory_id, exc)
        return None

    new_memory["supersedesId"] = memory_id
    return write_memory(new_memory)


def mark_stale(org_id: str, memory_type: str, scope_type: str, scope_id: str, max_age_days: int = 30):
    """Mark old memories as stale based on age."""
    db = _get_db()
    if not db:
        return

    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - max_age_days * 86400),
    )

    try:
        docs = (
            db.collection("hermesMemories")
            .where("orgId", "==", org_id)
            .where("memoryType", "==", memory_type)
            .where("scopeType", "==", scope_type)
            .where("scopeId", "==", scope_id)
            .where("status", "==", "active")
            .where("temporal.lastSeenAt", "<", cutoff)
            .limit(50)
            .stream()
        )

        batch = db.batch()
        count = 0
        for doc in docs:
            if doc.to_dict().get("retrieval", {}).get("pinned"):
                continue
            batch.update(doc.reference, {"status": "stale"})
            count += 1

        if count > 0:
            batch.commit()
            _invalidate_retrieval_cache(org_id, scope_id)
            logger.info("Marked %d stale memories (type=%s, scope=%s/%s)", count, memory_type, scope_type, scope_id)
    except Exception as exc:
        logger.error("Failed to mark stale: %s", exc)


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
        _invalidate_retrieval_cache(data.get("orgId", ""), "")
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
