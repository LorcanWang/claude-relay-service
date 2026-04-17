#!/usr/bin/env python3
"""
Hermes Worker — background process that consumes events from the Hermes queue,
extracts structured memories via Claude Haiku, and writes to Firestore + CRM.

Run: python hermes_worker.py
Or alongside main: set HERMES_WORKER_INLINE=true to run in-process.

Environment:
  HERMES_ENABLED         Enable Hermes (default true)
  HERMES_POLL_INTERVAL   Seconds between queue polls (default 5)
  HERMES_MODEL           Model for extraction (default claude-haiku-4-5-20251001)
  HERMES_RELAY_URL       Relay URL for API calls
  HERMES_AUTH_TOKEN       Auth token for relay
  GOOGLE_APPLICATION_CREDENTIALS  Firebase service account path
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from hermes_emitter import get_pending_events, get_queue_length
from hermes_extractor import extract_memories, extraction_to_memories
from hermes_store import (
    write_memories_batch,
    upsert_session,
    upsert_profile,
    get_profile,
    update_room_registry,
    get_room_cross_room_orgs,
)
from hermes_crm_bridge import memories_to_crm_actions, write_crm_actions
from hermes_campaign import normalize_snapshot, detect_anomalies, anomalies_to_memories

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("hermes.worker")

POLL_INTERVAL = int(os.environ.get("HERMES_POLL_INTERVAL", "5"))
RELAY_URL = os.environ.get("HERMES_RELAY_URL", os.environ.get("RELAY_BASE_URL", ""))
AUTH_TOKEN = os.environ.get("HERMES_AUTH_TOKEN", os.environ.get("RUNNER_KEY", ""))


def process_turn_batch(event: dict):
    """Process a batch of conversation turns — extract, store, bridge to CRM."""
    turns = event.get("turns", [])
    org_id = event.get("org_id", "")
    user_id = event.get("user_id", "")
    session_id = event.get("session_id", "")
    room_id = event.get("room_id")

    if not turns or not org_id:
        logger.warning("Skipping batch with missing data")
        return

    if not RELAY_URL or not AUTH_TOKEN:
        logger.warning("HERMES_RELAY_URL or HERMES_AUTH_TOKEN not set, skipping extraction")
        return

    logger.info(
        "Processing batch: session=%s turns=%d org=%s",
        session_id, len(turns), org_id,
    )

    extracted = extract_memories(
        turns=turns,
        org_id=org_id,
        session_id=session_id,
        room_id=room_id,
        base_url=RELAY_URL,
        auth_token=AUTH_TOKEN,
    )

    if not extracted:
        logger.info("No memories extracted from batch")
        return

    memories = extraction_to_memories(
        extracted=extracted,
        org_id=org_id,
        user_id=user_id,
        session_id=session_id,
        room_id=room_id,
        skill_configs=event.get("skill_configs"),
    )

    if memories:
        # Resolve the room's crossRoomOrgIds once per batch, stamp on every memory.
        # Room memories use the room's setting; non-room (session/user) memories
        # default to [org_id] — they don't bridge anyway.
        if room_id:
            cross_orgs = get_room_cross_room_orgs(room_id, org_id)
            for m in memories:
                if m.get("scopeType") == "room":
                    m["crossRoomOrgIds"] = cross_orgs
        saved_ids = write_memories_batch(memories)
        logger.info("Saved %d memories", len(saved_ids))

        # Update the room's entity registry from what we just observed.
        # Note: write_memories_batch doesn't return the prepared memories,
        # so we re-run the matcher here to get the refs + importance pairs.
        # This is cheap (microseconds) and keeps the registry in sync even if
        # the extractor pre-populated entityRefs.
        if room_id and memories:
            observed = []
            try:
                from hermes_entity_matcher import match_entities
                for m in memories:
                    refs = m.get("entityRefs")
                    if not refs:
                        text = f"{m.get('title', '')} {m.get('summary', '')}".strip()
                        refs = match_entities(text, org_id=org_id, skill_configs=event.get("skill_configs"))
                    importance = int(m.get("importance") or 50)
                    for r in refs:
                        observed.append({
                            "kind": r.get("kind"),
                            "id": r.get("id"),
                            "label": r.get("label"),
                            "importance": importance,
                        })
            except Exception as exc:
                logger.debug("Observed-entity collection failed: %s", exc)
            if observed:
                reg = update_room_registry(org_id, room_id, observed)
                if reg is not None:
                    active = sum(1 for v in reg.values() if v.get("status") == "active")
                    logger.info(
                        "Room registry updated room=%s observed=%d active=%d total=%d",
                        room_id, len(observed), active, len(reg),
                    )

    _update_session_record(session_id, org_id, user_id, room_id, turns, extracted)
    _update_user_profile(org_id, user_id, extracted)

    customer_id = event.get("customer_id") or _detect_customer_id(turns)
    if customer_id:
        crm_actions = memories_to_crm_actions(memories, org_id, customer_id)
        if crm_actions:
            written = write_crm_actions(crm_actions)
            logger.info("Created %d CRM actions for customer %s", written, customer_id)


_skill_usage_buffer: dict[str, list[dict]] = {}


def process_tool_executed(event: dict):
    """Track tool execution for skill co-usage and workflow patterns."""
    session_id = event.get("session_id", "")
    org_id = event.get("org_id", "")
    skill_name = event.get("skill_name", "")
    tool_name = event.get("tool_name", "")
    result_ok = event.get("result_ok", True)

    if not skill_name or not session_id:
        return

    if session_id not in _skill_usage_buffer:
        _skill_usage_buffer[session_id] = []

    _skill_usage_buffer[session_id].append({
        "skill": skill_name,
        "tool": tool_name,
        "ok": result_ok,
        "org_id": org_id,
        "ts": event.get("emitted_at", ""),
    })

    logger.debug("Skill tracked: %s/%s (session %s, total=%d)",
                 skill_name, tool_name, session_id,
                 len(_skill_usage_buffer[session_id]))


def process_session_closed(event: dict):
    """Handle session close — final summary extraction + mark session closed."""
    session_id = event.get("session_id", "")
    org_id = event.get("org_id", "")
    user_id = event.get("user_id", "")
    room_id = event.get("room_id")
    message_count = event.get("message_count", 0)

    logger.info("Session closed: %s (%d messages)", session_id, message_count)

    # Final extraction using the close event's bundled turns (if any)
    bundled_turns = event.get("final_turns", [])
    if bundled_turns and RELAY_URL and AUTH_TOKEN:
        logger.info("Final extraction for %d bundled turns", len(bundled_turns))
        extracted = extract_memories(
            turns=bundled_turns,
            org_id=org_id,
            session_id=session_id,
            room_id=room_id,
            base_url=RELAY_URL,
            auth_token=AUTH_TOKEN,
        )
        if extracted:
            memories = extraction_to_memories(
                extracted=extracted,
                org_id=org_id,
                user_id=user_id,
                session_id=session_id,
                room_id=room_id,
                skill_configs=event.get("skill_configs"),
            )
            if memories:
                if room_id:
                    cross_orgs = get_room_cross_room_orgs(room_id, org_id)
                    for m in memories:
                        if m.get("scopeType") == "room":
                            m["crossRoomOrgIds"] = cross_orgs
                write_memories_batch(memories)
                if room_id:
                    observed = []
                    try:
                        from hermes_entity_matcher import match_entities
                        for m in memories:
                            refs = m.get("entityRefs")
                            if not refs:
                                text = f"{m.get('title', '')} {m.get('summary', '')}".strip()
                                refs = match_entities(text, org_id=org_id, skill_configs=event.get("skill_configs"))
                            importance = int(m.get("importance") or 50)
                            for r in refs:
                                observed.append({
                                    "kind": r.get("kind"),
                                    "id": r.get("id"),
                                    "label": r.get("label"),
                                    "importance": importance,
                                })
                    except Exception as exc:
                        logger.debug("Observed-entity collection (close) failed: %s", exc)
                    if observed:
                        update_room_registry(org_id, room_id, observed)
            _update_user_profile(org_id, user_id, extracted)

    # Analyze skill usage patterns from this session
    _analyze_skill_patterns(session_id, org_id, user_id, room_id)

    # Mark stale action_items from this session/room scope
    from hermes_store import mark_stale, write_memory
    scope_type = "room" if room_id else "session"
    scope_id = room_id or session_id
    mark_stale(org_id, "action_item", scope_type, scope_id, max_age_days=14)

    upsert_session(session_id, {
        "orgId": org_id,
        "userId": user_id,
        "roomId": room_id,
        "messageCount": message_count,
        "status": "closed",
        "participantNames": event.get("participant_names", []),
    })


def _update_session_record(
    session_id: str,
    org_id: str,
    user_id: str,
    room_id: str | None,
    turns: list[dict],
    extracted: dict,
):
    tool_names_seen = set()
    for turn in turns:
        for t in turn.get("tool_names", []):
            tool_names_seen.add(t)

    try:
        from hermes_store import _get_db
        db = _get_db()
        if not db:
            return
        from google.cloud.firestore_v1 import ArrayUnion, Increment
        ref = db.collection("hermesSessions").document(session_id)
        ref.set({
            "orgId": org_id,
            "userId": user_id,
            "roomId": room_id,
            "toolsUsed": ArrayUnion(list(tool_names_seen)),
            "extractionCount": Increment(1),
            "lastSummary": extracted.get("session_summary", ""),
            "status": "active",
            "lastUpdated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, merge=True)
    except Exception as exc:
        logger.error("Failed to update session record: %s", exc)


def _update_user_profile(org_id: str, user_id: str, extracted: dict):
    preferences = extracted.get("preferences", [])
    if not preferences:
        return

    confirmed_prefs = [p for p in preferences if p.get("confidence", 0) >= 0.5]
    if not confirmed_prefs:
        return

    profile_key = f"user:{org_id}:{user_id}"
    existing = get_profile(profile_key)
    existing_prefs = existing.get("preferences", []) if existing else []

    existing_titles = {p.get("title", "").strip().lower() for p in existing_prefs}

    merged_prefs = list(existing_prefs)
    for pref in confirmed_prefs:
        title_lower = pref.get("title", "").strip().lower()
        if title_lower in existing_titles:
            for i, ep in enumerate(merged_prefs):
                if ep.get("title", "").strip().lower() == title_lower:
                    merged_prefs[i] = {
                        **ep,
                        "summary": pref["summary"],
                        "confidence": max(ep.get("confidence", 0.5), pref.get("confidence", 0.5)),
                        "observationCount": ep.get("observationCount", 1) + 1,
                    }
                    break
        else:
            merged_prefs.append({
                "title": pref.get("title", ""),
                "summary": pref["summary"],
                "confidence": pref.get("confidence", 0.5),
                "observationCount": 1,
            })

    pref_summary = "; ".join(
        p["summary"] for p in merged_prefs
        if p.get("observationCount", 1) >= 2 or p.get("confidence", 0) >= 0.7
    )

    upsert_profile(profile_key, {
        "orgId": org_id,
        "userId": user_id,
        "scopeType": "user",
        "preferences": merged_prefs,
        "preferencesSummary": pref_summary or "",
    })


def _analyze_skill_patterns(
    session_id: str,
    org_id: str,
    user_id: str,
    room_id: str | None,
):
    """Analyze skill co-usage and workflow sequences from a completed session."""
    usage = _skill_usage_buffer.pop(session_id, [])
    if len(usage) < 2:
        return

    from hermes_store import write_memory

    skills_used = []
    skill_counts: dict[str, int] = {}
    skill_success: dict[str, list[bool]] = {}

    for entry in usage:
        skill = entry["skill"]
        skills_used.append(skill)
        skill_counts[skill] = skill_counts.get(skill, 0) + 1
        if skill not in skill_success:
            skill_success[skill] = []
        skill_success[skill].append(entry.get("ok", True))

    unique_skills = list(dict.fromkeys(skills_used))

    if len(unique_skills) >= 2:
        workflow_seq = " → ".join(unique_skills)
        write_memory({
            "orgId": org_id,
            "scopeType": "user",
            "scopeId": user_id,
            "memoryType": "workflow_pattern",
            "title": f"Workflow: {' + '.join(unique_skills[:4])}",
            "summary": f"Used skills in sequence: {workflow_seq}",
            "importance": min(20 + len(unique_skills) * 10, 60),
            "confidence": 0.5,
            "relevanceTags": ["workflow"] + unique_skills,
            "skillIds": unique_skills,
            "source": {"kind": "skill_tracking", "sessionId": session_id},
        })
        logger.info("Workflow pattern saved: %s", workflow_seq)

    for skill, count in skill_counts.items():
        if count >= 3:
            success_rate = sum(1 for ok in skill_success[skill] if ok) / len(skill_success[skill])
            write_memory({
                "orgId": org_id,
                "scopeType": "user",
                "scopeId": user_id,
                "memoryType": "skill_pattern",
                "title": f"Heavy use: {skill}",
                "summary": f"{skill} called {count} times (success rate: {success_rate:.0%})",
                "importance": 30,
                "confidence": 0.6,
                "relevanceTags": ["skill_usage", skill],
                "skillIds": [skill],
                "source": {"kind": "skill_tracking", "sessionId": session_id},
            })

    profile_key = f"user:{org_id}:{user_id}"
    existing = get_profile(profile_key)
    existing_skills = existing.get("frequentSkills", []) if existing else []

    skill_set = set(existing_skills)
    for skill in unique_skills:
        skill_set.add(skill)

    upsert_profile(profile_key, {
        "orgId": org_id,
        "userId": user_id,
        "scopeType": "user",
        "frequentSkills": sorted(skill_set),
        "lastWorkflow": " → ".join(unique_skills),
    })


def _detect_customer_id(turns: list[dict]) -> str | None:
    """
    Try to detect a customer context from the conversation.
    For now returns None — will be wired when room/session metadata
    includes customer linking.
    """
    return None


def process_campaign_snapshot(event: dict):
    """Ingest and analyze a campaign performance snapshot."""
    org_id = event.get("org_id", "")
    platform = event.get("platform", "")
    raw_snapshot = event.get("snapshot", {})
    customer_id = event.get("customer_id", "")

    if not raw_snapshot or not org_id:
        return

    normalized = normalize_snapshot(raw_snapshot, platform)
    if not normalized:
        logger.warning("Could not normalize snapshot for %s", platform)
        return

    normalized["orgId"] = org_id
    normalized["customerId"] = customer_id

    try:
        from hermes_store import _get_db
        db = _get_db()
        if db:
            doc_id = f"{org_id}_{platform}_{normalized.get('campaignId', '')}_{normalized.get('date', '')}"
            db.collection("hermesCampaignSnapshots").document(doc_id).set(normalized)
            logger.info("Campaign snapshot saved: %s", doc_id)

            # Check for anomalies against last snapshot as baseline
            prev_docs = (
                db.collection("hermesCampaignSnapshots")
                .where("orgId", "==", org_id)
                .where("platform", "==", platform)
                .where("campaignId", "==", normalized.get("campaignId", ""))
                .order_by("capturedAt", direction="DESCENDING")
                .limit(2)
                .stream()
            )
            prev_list = [d.to_dict() for d in prev_docs]
            if len(prev_list) >= 2:
                anomalies = detect_anomalies(prev_list[0], prev_list[1])
                if anomalies:
                    memories = anomalies_to_memories(anomalies, org_id, customer_id)
                    write_memories_batch(memories)
                    logger.info("Detected %d campaign anomalies", len(anomalies))
    except Exception as exc:
        logger.error("Campaign snapshot processing failed: %s", exc)


EVENT_HANDLERS = {
    "turn_batch_ready": process_turn_batch,
    "tool_executed": process_tool_executed,
    "session_closed": process_session_closed,
    "campaign_snapshot_ingested": process_campaign_snapshot,
}


def process_events(events: list[dict]):
    for event in events:
        event_type = event.get("type", "")
        handler = EVENT_HANDLERS.get(event_type)
        if handler:
            try:
                handler(event)
            except Exception as exc:
                logger.exception("Error processing event %s: %s", event_type, exc)
        else:
            logger.debug("Unknown event type: %s", event_type)


def run_poll_loop():
    """Main polling loop — runs forever, polls Redis queue."""
    logger.info("Hermes Worker started (poll interval=%ds)", POLL_INTERVAL)
    logger.info("Relay URL: %s", RELAY_URL[:50] if RELAY_URL else "NOT SET")

    while True:
        try:
            queue_len = get_queue_length()
            if queue_len > 0:
                logger.info("Queue has %d events", queue_len)
                events = get_pending_events(max_count=20)
                if events:
                    process_events(events)
        except KeyboardInterrupt:
            logger.info("Hermes Worker shutting down")
            break
        except Exception as exc:
            logger.exception("Worker loop error: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_poll_loop()
