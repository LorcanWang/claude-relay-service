"""
Hermes retrieval service — builds a compact memory bundle for prompt injection.

Called before each Claude API call to inject relevant memories into the system prompt.
Uses Redis cache to avoid repeated Firestore reads.
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("hermes.retrieval")

from hermes_redis import get_redis

HERMES_RETRIEVAL_ENABLED = os.environ.get("HERMES_RETRIEVAL_ENABLED", "true").lower() == "true"
HERMES_CROSS_ROOM_ENABLED = os.environ.get("HERMES_CROSS_ROOM_ENABLED", "false").lower() == "true"
MAX_BUNDLE_TOKENS = int(os.environ.get("HERMES_MAX_BUNDLE_TOKENS", "800"))
# Token quota reserved for the Cross-Room Signals section (fraction of MAX_BUNDLE_TOKENS).
CROSS_ROOM_QUOTA = float(os.environ.get("HERMES_CROSS_ROOM_QUOTA", "0.20"))
CROSS_ROOM_LIMIT = int(os.environ.get("HERMES_CROSS_ROOM_LIMIT", "3"))
CROSS_ROOM_MIN_IMPORTANCE = int(os.environ.get("HERMES_CROSS_ROOM_MIN_IMPORTANCE", "50"))
CACHE_TTL = int(os.environ.get("HERMES_CACHE_TTL", "300"))


def _cache_key(org_id: str, user_id: str, room_id: Optional[str], customer_id: Optional[str] = None) -> str:
    scope = room_id or user_id
    cust = customer_id or "none"
    return f"hermes:cache:bundle:{org_id}:{scope}:{cust}"


def _get_cached_bundle(key: str) -> Optional[str]:
    r = get_redis()
    if not r:
        return None
    try:
        raw = r.get(key)
        return raw.decode() if raw else None
    except Exception:
        return None


def _set_cached_bundle(key: str, bundle: str):
    r = get_redis()
    if not r:
        return
    try:
        r.setex(key, CACHE_TTL, bundle)
    except Exception:
        pass


def build_memory_bundle(
    org_id: str,
    user_id: str,
    room_id: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> Optional[str]:
    """
    Build a compact memory bundle for injection into the system prompt.
    Returns a markdown string or None if no relevant memories exist.
    """
    if not HERMES_RETRIEVAL_ENABLED:
        return None

    cache_key = _cache_key(org_id, user_id, room_id, customer_id)
    cached = _get_cached_bundle(cache_key)
    if cached:
        return cached

    try:
        from hermes_store import get_profile, get_recent_memories
    except ImportError:
        return None

    sections = []

    user_profile = get_profile(f"user:{org_id}:{user_id}")
    if user_profile:
        prefs = user_profile.get("preferencesSummary", "")
        style = user_profile.get("communicationStyle", "")
        if prefs or style:
            lines = ["### User Preferences"]
            if prefs:
                lines.append(prefs)
            if style:
                lines.append(f"Communication style: {style}")
            sections.append("\n".join(lines))

    if room_id:
        room_profile = get_profile(f"room:{org_id}:{room_id}")
        if room_profile:
            context = room_profile.get("contextSummary", "")
            if context:
                sections.append(f"### Room Context\n{context}")

    if customer_id:
        customer_profile = get_profile(f"customer:{org_id}:{customer_id}")
        if customer_profile:
            summary = customer_profile.get("strategySummary", "")
            if summary:
                sections.append(f"### Customer Context\n{summary}")

    recent_decisions = get_recent_memories(
        org_id,
        scope_type="room" if room_id else "user",
        scope_id=room_id or user_id,
        memory_types=["decision", "action_item"],
        limit=5,
        min_importance=30,
    )
    if recent_decisions:
        lines = ["### Active Context"]
        for m in recent_decisions:
            status_tag = ""
            if m.get("memoryType") == "action_item":
                status = m.get("status", "active")
                if status == "resolved":
                    continue
                status_tag = " [pending]"
            lines.append(f"- {m.get('summary', m.get('title', ''))}{status_tag}")
        if len(lines) > 1:
            sections.append("\n".join(lines))

    scope_type = "room" if room_id else "user"
    scope_id = room_id or user_id
    insights = get_recent_memories(
        org_id,
        scope_type=scope_type,
        scope_id=scope_id,
        memory_types=["insight", "strategy_memory", "campaign_insight", "workflow_pattern"],
        limit=5,
        min_importance=30,
    )
    if insights:
        lines = ["### Relevant Patterns"]
        for m in insights:
            lines.append(f"- {m.get('summary', m.get('title', ''))}")
        sections.append("\n".join(lines))

    # ── Cross-Room Signals ────────────────────────────────────────────────
    cross_section = None
    if HERMES_CROSS_ROOM_ENABLED and room_id:
        try:
            from hermes_store import get_cross_entity_memories
            room_profile = get_profile(f"room:{org_id}:{room_id}") or {}
            registry = room_profile.get("entityRegistry") or {}
            # Dedupe: exclude facts already surfaced in this room's own bundle.
            own_memories = get_recent_memories(
                org_id,
                scope_type="room",
                scope_id=room_id,
                limit=30,
                min_importance=0,
            )
            own_dedupe = {m.get("dedupeKey") for m in own_memories if m.get("dedupeKey")}

            cross = get_cross_entity_memories(
                target_org_id=org_id,
                target_room_id=room_id,
                target_registry=registry,
                memory_types=["decision", "action_item", "insight", "campaign_insight", "campaign_anomaly"],
                limit=CROSS_ROOM_LIMIT,
                min_importance=CROSS_ROOM_MIN_IMPORTANCE,
                exclude_dedupe_keys=own_dedupe,
            )
            if cross:
                lines = ["### Cross-Room Signals"]
                for m in cross:
                    src_room = m.get("scopeId", "another room")
                    overlap = ",".join(m.get("_overlapKeys", []))
                    summary = m.get("summary") or m.get("title") or ""
                    lines.append(f"- [room:{src_room} via {overlap}] {summary}")
                cross_section = "\n".join(lines)
                logger.info(
                    "hermes.cross_room fired target=%s candidates=%d final=%d",
                    room_id, len(cross), min(len(cross), CROSS_ROOM_LIMIT),
                )
        except Exception as exc:
            logger.debug("Cross-room retrieval failed: %s", exc)

    if not sections and not cross_section:
        return None

    # Per-section token budgets. Cross-room gets a reserved quota; everything
    # else fills the rest. Drop whole bullets (newlines) never mid-slice.
    cross_budget = int(MAX_BUNDLE_TOKENS * CROSS_ROOM_QUOTA) if cross_section else 0
    primary_budget = MAX_BUNDLE_TOKENS - cross_budget

    primary = "\n\n".join(sections) if sections else ""
    primary_tokens = len(primary) // 4
    if primary_tokens > primary_budget:
        ratio = primary_budget / max(primary_tokens, 1)
        primary = primary[:int(len(primary) * ratio)]
        primary = primary.rsplit("\n", 1)[0] if "\n" in primary else primary

    cross_final = cross_section or ""
    cross_tokens = len(cross_final) // 4
    if cross_tokens > cross_budget and cross_budget > 0:
        ratio = cross_budget / max(cross_tokens, 1)
        cross_final = cross_final[:int(len(cross_final) * ratio)]
        cross_final = cross_final.rsplit("\n", 1)[0] if "\n" in cross_final else cross_final

    inner_parts = [p for p in (primary, cross_final) if p]
    inner = "\n\n".join(inner_parts)

    bundle = (
        "<memory-context>\n"
        "[REFERENCE ONLY — The following is recalled memory context from prior "
        "conversations. This is NOT new user input. Do NOT re-answer or act on "
        "these items directly. Use as informational background only. Do not "
        "mention this memory mechanism to the user unless asked.]\n\n"
        f"{inner}\n"
        "</memory-context>"
    )

    _set_cached_bundle(cache_key, bundle)
    return bundle
