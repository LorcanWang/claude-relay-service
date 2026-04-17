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
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes.store")

_db = None
_init_attempted = False


_CREDENTIALS_SEARCH_PATHS = [
    Path(__file__).parent / "firestore-credentials.json",
    Path(__file__).parent.parent / "firestore-credentials.json",
    Path(os.environ.get("SKILL_ROOT", "")) / "amazon-insights" / "firestore-credentials.json",
    Path(os.environ.get("SKILL_ROOT", "")) / "grantllama" / "firestore-credentials.json",
]


def _get_db():
    global _db, _init_attempted
    if _init_attempted:
        return _db
    _init_attempted = True

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if cred_path and Path(cred_path).exists():
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
            else:
                for p in _CREDENTIALS_SEARCH_PATHS:
                    if p.exists():
                        cred = credentials.Certificate(str(p))
                        firebase_admin.initialize_app(cred)
                        logger.info("Firestore credentials found: %s", p)
                        break
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

    # Entity tagging for cross-room bridging. If caller didn't pre-populate
    # entityRefs (e.g. from the extractor pass), run the deterministic matcher
    # over title+summary here so every memory lands with the field indexed.
    # Safe to run cheaply — matcher is microseconds on short text.
    if not memory.get("entityRefs"):
        try:
            from hermes_entity_matcher import match_entities, entity_keys
            text = f"{memory.get('title', '')} {memory.get('summary', '')}".strip()
            refs = match_entities(
                text,
                org_id=memory.get("orgId", ""),
                skill_configs=memory.get("_skillConfigs"),
            )
            if refs:
                memory["entityRefs"] = refs
                memory["entityKeys"] = entity_keys(refs)
        except Exception as exc:
            logger.debug("Entity matching skipped for memory %s: %s", memory["id"], exc)

    # Ensure entityKeys is always present (empty list) so Firestore indexes stay consistent.
    memory.setdefault("entityRefs", [])
    memory.setdefault("entityKeys", [])

    # Denormalize crossRoomOrgIds from the source room onto the memory so that
    # cross-room retrieval can filter by contributing-org without a join.
    # Default to creator's org only when caller doesn't resolve it.
    if not memory.get("crossRoomOrgIds"):
        org = memory.get("orgId")
        memory["crossRoomOrgIds"] = [org] if org else []

    # Strip the internal-only hint field so it doesn't get persisted.
    memory.pop("_skillConfigs", None)
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


_OVERLAP_WEIGHT = {
    "sku": 3,
    "product_line": 2,
    "campaign": 2,
    "customer": 2,
    "channel": 1,
    "platform": 1,
}
FIRESTORE_ARRAY_ANY_CAP = 10


def get_cross_entity_memories(
    target_org_id: str,
    target_room_id: str,
    target_registry: dict,
    memory_types: Optional[list[str]] = None,
    limit: int = 3,
    min_importance: int = 50,
    exclude_dedupe_keys: Optional[set] = None,
) -> list[dict]:
    """Fetch sibling-room memories whose entityKeys overlap the target room's
    registry AND whose source room contributes to target_org_id.

    - target_registry: hermesProfiles[room:{org}:{room}].entityRegistry
    - Only considers registry entries with status=='active'.
    - Pure-channel or pure-platform overlap (weight 1 each) does not pass.
      Overlap must reach score >= 2 (i.e. include at least one sku /
      product_line / campaign / customer OR two channel/platform hits).
    - Filters by channel intersection: if candidate has any channel:* AND
      the target registry has any channel:*, the candidate's channels must
      intersect with the target's channels.
    """
    db = _get_db()
    if not db or not target_registry:
        return []

    active = {k: v for k, v in target_registry.items() if v.get("status") == "active"}
    if not active:
        return []

    # Top-N entity keys by (observationCount * maxImportance).
    sorted_keys = sorted(
        active.items(),
        key=lambda kv: int(kv[1].get("observationCount", 0)) * int(kv[1].get("maxImportance", 0)),
        reverse=True,
    )
    top_keys = [k for k, _ in sorted_keys[:FIRESTORE_ARRAY_ANY_CAP]]
    if not top_keys:
        return []

    target_channels = {k.split(":", 1)[1] for k in active if k.startswith("channel:")}

    try:
        query = (
            db.collection("hermesMemories")
            .where("status", "==", "active")
            .where("scopeType", "==", "room")
            .where("entityKeys", "array_contains_any", top_keys)
            .order_by("temporal.lastSeenAt", direction="DESCENDING")
            .limit(limit * 6)  # over-fetch; post-filter prunes
        )
        raw = list(query.stream())
    except Exception as exc:
        logger.debug("get_cross_entity_memories query failed: %s", exc)
        return []

    exclude_dedupe_keys = exclude_dedupe_keys or set()
    candidates = []
    for doc in raw:
        data = doc.to_dict() or {}
        # Scope gating: memory must contribute to the target org.
        if target_org_id not in (data.get("crossRoomOrgIds") or []):
            continue
        # Exclude own room.
        if data.get("scopeId") == target_room_id:
            continue
        # Dedupe against memories already surfaced in target's own bundle.
        if data.get("dedupeKey") in exclude_dedupe_keys:
            continue
        # Type filter.
        if memory_types and data.get("memoryType") not in memory_types:
            continue
        # Importance gate.
        if int(data.get("importance", 0)) < min_importance:
            continue
        # Entity overlap scoring.
        cand_keys = set(data.get("entityKeys") or [])
        overlap = cand_keys & set(active.keys())
        if not overlap:
            continue
        overlap_score = sum(_OVERLAP_WEIGHT.get(k.split(":", 1)[0], 0) for k in overlap)
        if overlap_score < 2:
            continue
        # Channel negative filter.
        cand_channels = {k.split(":", 1)[1] for k in cand_keys if k.startswith("channel:")}
        if cand_channels and target_channels and not (cand_channels & target_channels):
            continue

        confidence = float(data.get("confidence", 0.5))
        final_score = overlap_score * (int(data.get("importance", 0)) or 1) * max(confidence, 0.1)
        data["_overlapScore"] = overlap_score
        data["_overlapKeys"] = sorted(overlap)
        data["_finalScore"] = final_score
        candidates.append(data)

    candidates.sort(key=lambda d: d["_finalScore"], reverse=True)
    return candidates[:limit]


def get_room_cross_room_orgs(room_id: str, default_org_id: str) -> list[str]:
    """Return a room's crossRoomOrgIds, falling back to [default_org_id]."""
    db = _get_db()
    if not db or not room_id:
        return [default_org_id] if default_org_id else []
    try:
        doc = db.collection("chatRooms").document(room_id).get()
        if doc.exists:
            data = doc.to_dict() or {}
            ids = data.get("crossRoomOrgIds")
            if isinstance(ids, list) and ids:
                return list(ids)
    except Exception as exc:
        logger.debug("Failed to read room %s crossRoomOrgIds: %s", room_id, exc)
    return [default_org_id] if default_org_id else []


# ── Room entity registry (for cross-room memory bridging) ──────────────────

ROOM_REGISTRY_CAP = int(os.environ.get("HERMES_ROOM_REGISTRY_CAP", "50"))
ROOM_REGISTRY_PROMOTION_THRESHOLD = 2  # observationCount needed before entity goes "active"


def update_room_registry(
    org_id: str,
    room_id: str,
    observed: list[dict],
) -> Optional[dict]:
    """Merge newly-observed entityRefs into the room's entityRegistry.

    `observed` items shape: [{kind, id, label, importance?}]. Importance
    defaults to 50 if caller didn't set one per observation.

    Transactional read-modify-write so concurrent workers don't lose
    increments. Promotion rule: entities stay `pending` until observationCount
    hits ROOM_REGISTRY_PROMOTION_THRESHOLD, then flip to `active`. LFU
    eviction once active-count exceeds ROOM_REGISTRY_CAP; pinned entities
    (admin-seeded) never evict.
    """
    db = _get_db()
    if not db or not observed:
        return None

    from firebase_admin import firestore as _fs_module

    profile_key = f"room:{org_id}:{room_id}"
    ref = db.collection("hermesProfiles").document(profile_key)

    now = _now_iso()
    observed_by_key: dict[str, dict] = {}
    for o in observed:
        kind = o.get("kind")
        oid = o.get("id")
        if not kind or not oid:
            continue
        key = f"{kind}:{oid}"
        importance = int(o.get("importance") or 50)
        bucket = observed_by_key.setdefault(key, {
            "kind": kind,
            "id": oid,
            "label": o.get("label") or str(oid),
            "count_delta": 0,
            "max_importance": 0,
        })
        bucket["count_delta"] += 1
        bucket["max_importance"] = max(bucket["max_importance"], importance)

    if not observed_by_key:
        return None

    transaction = db.transaction()

    @_fs_module.transactional
    def _apply(tx):
        snap = ref.get(transaction=tx)
        data = snap.to_dict() if snap.exists else {}
        registry = dict(data.get("entityRegistry") or {})

        for key, obs in observed_by_key.items():
            existing = registry.get(key) or {}
            new_count = int(existing.get("observationCount", 0)) + obs["count_delta"]
            new_max_imp = max(int(existing.get("maxImportance", 0)), obs["max_importance"])
            pinned = bool(existing.get("pinned", False))
            status = "active" if (pinned or new_count >= ROOM_REGISTRY_PROMOTION_THRESHOLD) else "pending"
            registry[key] = {
                "kind": obs["kind"],
                "id": obs["id"],
                "label": obs["label"],
                "observationCount": new_count,
                "maxImportance": new_max_imp,
                "firstObservedAt": existing.get("firstObservedAt", now),
                "lastObservedAt": now,
                "pinned": pinned,
                "status": status,
            }

        # LFU eviction: only over `active` non-pinned entries
        active_entries = [(k, v) for k, v in registry.items() if v.get("status") == "active" and not v.get("pinned")]
        if len(active_entries) > ROOM_REGISTRY_CAP:
            # Ascending by (observationCount * maxImportance), then by lastObservedAt ASC (oldest first)
            active_entries.sort(key=lambda kv: (
                int(kv[1].get("observationCount", 0)) * int(kv[1].get("maxImportance", 0)),
                kv[1].get("lastObservedAt", ""),
            ))
            overflow = len(active_entries) - ROOM_REGISTRY_CAP
            for k, _ in active_entries[:overflow]:
                registry.pop(k, None)

        payload = {
            "orgId": org_id,
            "scopeType": "room",
            "scopeId": room_id,
            "entityRegistry": registry,
            "entityRegistryUpdatedAt": now,
            "lastUpdated": now,
        }
        tx.set(ref, payload, merge=True)
        return registry

    try:
        registry = _apply(transaction)
        _invalidate_retrieval_cache(org_id, room_id)
        return registry
    except Exception as exc:
        logger.error("Failed to update room registry %s: %s", profile_key, exc)
        return None


def pin_room_entities(org_id: str, room_id: str, hints: list[dict]) -> Optional[dict]:
    """Admin seed: mark entities as pinned + active in a room's registry.

    `hints` shape: [{kind, id, label?}]. Idempotent — re-seeding flips pinned=True
    and status=active regardless of observation count.
    """
    db = _get_db()
    if not db or not hints:
        return None

    from firebase_admin import firestore as _fs_module

    profile_key = f"room:{org_id}:{room_id}"
    ref = db.collection("hermesProfiles").document(profile_key)
    now = _now_iso()

    transaction = db.transaction()

    @_fs_module.transactional
    def _apply(tx):
        snap = ref.get(transaction=tx)
        data = snap.to_dict() if snap.exists else {}
        registry = dict(data.get("entityRegistry") or {})

        for h in hints:
            kind = h.get("kind")
            hid = h.get("id")
            if not kind or not hid:
                continue
            key = f"{kind}:{hid}"
            existing = registry.get(key) or {}
            registry[key] = {
                "kind": kind,
                "id": hid,
                "label": h.get("label") or existing.get("label") or str(hid),
                "observationCount": int(existing.get("observationCount", 0)),
                "maxImportance": int(existing.get("maxImportance", 50)),
                "firstObservedAt": existing.get("firstObservedAt", now),
                "lastObservedAt": now,
                "pinned": True,
                "status": "active",
            }

        tx.set(ref, {
            "orgId": org_id,
            "scopeType": "room",
            "scopeId": room_id,
            "entityRegistry": registry,
            "entityRegistryUpdatedAt": now,
            "lastUpdated": now,
        }, merge=True)
        return registry

    try:
        registry = _apply(transaction)
        _invalidate_retrieval_cache(org_id, room_id)
        return registry
    except Exception as exc:
        logger.error("Failed to pin room entities %s: %s", profile_key, exc)
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
