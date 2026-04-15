"""
Hermes CRM bridge — writes AI-extracted memories into the existing crmActions collection.

Maps extracted decisions, action items, and summaries to CRM events/tasks/notes
with source='ai_agent' and actorType='agent'.
"""

import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger("hermes.crm_bridge")


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def memories_to_crm_actions(
    memories: list[dict],
    org_id: str,
    customer_id: Optional[str] = None,
) -> list[dict]:
    """
    Convert Hermes memories into crmActions documents.
    Only creates CRM actions for high-confidence, customer-relevant memories.
    """
    if not customer_id:
        return []

    actions = []
    now = _now_iso()

    for memory in memories:
        importance = memory.get("importance", 0)
        confidence = memory.get("confidence", 0)
        memory_type = memory.get("memoryType", "")

        if confidence < 0.6:
            continue

        if memory_type == "session_summary" or memory_type == "room_summary":
            if importance >= 20:
                actions.append({
                    "id": str(uuid.uuid4()),
                    "orgId": org_id,
                    "customerId": customer_id,
                    "recordKind": "note",
                    "type": "ai_summary",
                    "visibility": "internal",
                    "title": memory.get("title", "AI Session Summary"),
                    "description": memory.get("summary", ""),
                    "source": "ai_agent",
                    "actorType": "agent",
                    "actorId": "hermes",
                    "occurredAt": now,
                    "attachmentCount": 0,
                    "commentCount": 0,
                    "tags": memory.get("relevanceTags", []),
                    "isArchived": False,
                    "relatedRefs": [],
                    "createdAt": now,
                    "updatedAt": now,
                })

        elif memory_type == "decision":
            if importance >= 40:
                actions.append({
                    "id": str(uuid.uuid4()),
                    "orgId": org_id,
                    "customerId": customer_id,
                    "recordKind": "event",
                    "type": "milestone",
                    "visibility": "internal",
                    "title": memory.get("title", "Decision Made"),
                    "description": memory.get("summary", ""),
                    "source": "ai_agent",
                    "actorType": "agent",
                    "actorId": "hermes",
                    "occurredAt": now,
                    "attachmentCount": 0,
                    "commentCount": 0,
                    "tags": ["decision"] + memory.get("relevanceTags", []),
                    "isArchived": False,
                    "relatedRefs": [],
                    "createdAt": now,
                    "updatedAt": now,
                })

        elif memory_type == "action_item":
            if importance >= 30:
                actions.append({
                    "id": str(uuid.uuid4()),
                    "orgId": org_id,
                    "customerId": customer_id,
                    "recordKind": "task",
                    "type": "follow_up",
                    "visibility": "internal",
                    "title": memory.get("title", "Follow-up"),
                    "description": memory.get("summary", ""),
                    "source": "ai_agent",
                    "actorType": "agent",
                    "actorId": "hermes",
                    "occurredAt": now,
                    "status": "open",
                    "priority": "medium" if importance < 60 else "high",
                    "attachmentCount": 0,
                    "commentCount": 0,
                    "tags": ["action_item"] + memory.get("relevanceTags", []),
                    "isArchived": False,
                    "relatedRefs": [],
                    "createdAt": now,
                    "updatedAt": now,
                })

        elif memory_type == "insight" or memory_type == "campaign_insight":
            if importance >= 50:
                actions.append({
                    "id": str(uuid.uuid4()),
                    "orgId": org_id,
                    "customerId": customer_id,
                    "recordKind": "note",
                    "type": "analysis",
                    "visibility": "internal",
                    "title": memory.get("title", "AI Insight"),
                    "description": memory.get("summary", ""),
                    "source": "ai_agent",
                    "actorType": "agent",
                    "actorId": "hermes",
                    "occurredAt": now,
                    "attachmentCount": 0,
                    "commentCount": 0,
                    "tags": memory.get("relevanceTags", []),
                    "isArchived": False,
                    "relatedRefs": [],
                    "createdAt": now,
                    "updatedAt": now,
                })

    return actions


def write_crm_actions(actions: list[dict]) -> int:
    """Write CRM actions to Firestore crmActions collection."""
    if not actions:
        return 0

    try:
        from hermes_store import _get_db
        db = _get_db()
        if not db:
            logger.warning("Firestore unavailable, CRM actions not written")
            return 0

        batch = db.batch()
        for action in actions:
            ref = db.collection("crmActions").document(action["id"])
            batch.set(ref, action)

        batch.commit()
        logger.info("Wrote %d CRM actions", len(actions))
        return len(actions)

    except Exception as exc:
        logger.error("Failed to write CRM actions: %s", exc)
        return 0
