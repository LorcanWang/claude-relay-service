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
from hermes_store import write_memories_batch, upsert_session, upsert_profile
from hermes_crm_bridge import memories_to_crm_actions, write_crm_actions

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
    )

    if memories:
        saved_ids = write_memories_batch(memories)
        logger.info("Saved %d memories", len(saved_ids))

    _update_session_record(session_id, org_id, user_id, room_id, turns, extracted)
    _update_user_profile(org_id, user_id, extracted)

    customer_id = _detect_customer_id(turns)
    if customer_id:
        crm_actions = memories_to_crm_actions(memories, org_id, customer_id)
        if crm_actions:
            written = write_crm_actions(crm_actions)
            logger.info("Created %d CRM actions for customer %s", written, customer_id)


def process_tool_executed(event: dict):
    """Track tool execution patterns for skill learning."""
    logger.debug("Tool executed: %s/%s", event.get("skill_name"), event.get("tool_name"))


def process_session_closed(event: dict):
    """Handle session close — write final summary."""
    session_id = event.get("session_id", "")
    org_id = event.get("org_id", "")
    user_id = event.get("user_id", "")
    room_id = event.get("room_id")
    message_count = event.get("message_count", 0)

    logger.info("Session closed: %s (%d messages)", session_id, message_count)

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

    pref_lines = [p["summary"] for p in preferences if p.get("confidence", 0) >= 0.5]
    if not pref_lines:
        return

    profile_key = f"user:{org_id}:{user_id}"
    upsert_profile(profile_key, {
        "orgId": org_id,
        "userId": user_id,
        "scopeType": "user",
        "preferencesSummary": "; ".join(pref_lines),
    })


def _detect_customer_id(turns: list[dict]) -> str | None:
    """
    Try to detect a customer context from the conversation.
    For now returns None — will be wired when room/session metadata
    includes customer linking.
    """
    return None


EVENT_HANDLERS = {
    "turn_batch_ready": process_turn_batch,
    "tool_executed": process_tool_executed,
    "session_closed": process_session_closed,
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
