"""
Hermes extractor — uses Claude Haiku to extract structured memories from conversation turns.

Called by hermes_worker when a batch of non-trivial turns is ready.
Produces structured memories: decisions, action items, preferences, insights.
"""

import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("hermes.extractor")

EXTRACTION_MODEL = os.environ.get("HERMES_MODEL", "claude-haiku-4-5-20251001")
EXTRACTION_MAX_TOKENS = 2048

EXTRACTION_SYSTEM = """You are Hermes, a memory extraction agent. You analyze conversation turns and extract structured knowledge.

For each batch of conversation turns, extract ALL of the following that apply:

1. **decisions** — things that were decided, agreed upon, or committed to
2. **action_items** — follow-ups, todos, tasks to complete
3. **preferences** — user preferences, styles, habits revealed
4. **insights** — observations about campaigns, strategies, patterns
5. **session_summary** — brief summary of what happened in these turns

Output valid JSON with this exact structure:
{
  "decisions": [{"title": "...", "summary": "...", "actors": ["name"], "importance": 0-100}],
  "action_items": [{"title": "...", "summary": "...", "assignee": "name or null", "importance": 0-100}],
  "preferences": [{"title": "...", "summary": "...", "user": "name", "confidence": 0.0-1.0}],
  "insights": [{"title": "...", "summary": "...", "tags": ["campaign", "strategy", etc], "importance": 0-100}],
  "session_summary": "One paragraph summary of these turns",
  "participants": [{"name": "...", "contribution": "brief description of what they did"}]
}

Rules:
- Only extract genuinely meaningful items, not noise
- Importance: 0-30 low, 30-60 medium, 60-100 high
- Confidence for preferences: 0.3 tentative, 0.6 likely, 0.9+ certain
- Include actor/user names when mentioned (from [Name]: prefixes in room messages)
- If nothing meaningful, return empty arrays and a brief summary
- Keep summaries concise (1-2 sentences max)"""


def _build_transcript(turns: list[dict]) -> str:
    lines = []
    for turn in turns:
        sender = turn.get("sender_name") or turn.get("user_id", "User")
        user_text = turn.get("user_text", "")
        assistant_text = turn.get("assistant_text", "")
        tools = turn.get("tool_names", [])

        if user_text:
            lines.append(f"[{sender}]: {user_text}")
        if tools:
            lines.append(f"[Agent used tools: {', '.join(tools)}]")
        if assistant_text:
            lines.append(f"[Assistant]: {assistant_text}")
        lines.append("")

    return "\n".join(lines)


def extract_memories(
    turns: list[dict],
    org_id: str,
    session_id: str,
    room_id: Optional[str],
    base_url: str,
    auth_token: str,
) -> Optional[dict]:
    """
    Extract structured memories from a batch of conversation turns.
    Uses Claude Haiku via the relay for cost efficiency.
    Returns parsed extraction dict or None on failure.
    """
    if not turns:
        return None

    transcript = _build_transcript(turns)
    if len(transcript.strip()) < 20:
        return None

    user_prompt = f"Extract memories from these conversation turns:\n\n{transcript}"

    try:
        url = f"{base_url.rstrip('/')}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": EXTRACTION_MODEL,
            "max_tokens": EXTRACTION_MAX_TOKENS,
            "system": EXTRACTION_SYSTEM,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)

        if resp.status_code != 200:
            logger.error("Extraction API error %d: %s", resp.status_code, resp.text[:300])
            return None

        data = resp.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        if not text.strip():
            return None

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        extracted = json.loads(text)
        logger.info(
            "Extraction complete: %d decisions, %d actions, %d prefs, %d insights",
            len(extracted.get("decisions", [])),
            len(extracted.get("action_items", [])),
            len(extracted.get("preferences", [])),
            len(extracted.get("insights", [])),
        )
        return extracted

    except json.JSONDecodeError as exc:
        logger.error("Extraction JSON parse error: %s", exc)
        return None
    except Exception as exc:
        logger.error("Extraction failed: %s", exc)
        return None


def extraction_to_memories(
    extracted: dict,
    org_id: str,
    user_id: str,
    session_id: str,
    room_id: Optional[str],
) -> list[dict]:
    """Convert extraction output into hermesMemories documents."""
    memories = []
    scope_type = "room" if room_id else "session"
    scope_id = room_id or session_id

    source_base = {
        "kind": "extraction",
        "sessionId": session_id,
        "roomId": room_id,
    }

    for decision in extracted.get("decisions", []):
        memories.append({
            "orgId": org_id,
            "scopeType": scope_type,
            "scopeId": scope_id,
            "memoryType": "decision",
            "title": decision["title"],
            "summary": decision["summary"],
            "importance": decision.get("importance", 50),
            "confidence": 0.8,
            "relevanceTags": ["decision"],
            "source": source_base,
            "actorRefs": [{"displayName": a} for a in decision.get("actors", [])],
        })

    for item in extracted.get("action_items", []):
        memories.append({
            "orgId": org_id,
            "scopeType": scope_type,
            "scopeId": scope_id,
            "memoryType": "action_item",
            "title": item["title"],
            "summary": item["summary"],
            "importance": item.get("importance", 40),
            "confidence": 0.7,
            "relevanceTags": ["action_item"],
            "source": source_base,
            "actorRefs": [{"displayName": item["assignee"]}] if item.get("assignee") else [],
        })

    for pref in extracted.get("preferences", []):
        memories.append({
            "orgId": org_id,
            "scopeType": "user",
            "scopeId": pref.get("user", user_id),
            "memoryType": "user_preference",
            "title": pref["title"],
            "summary": pref["summary"],
            "importance": 60,
            "confidence": pref.get("confidence", 0.5),
            "relevanceTags": ["preference"],
            "source": source_base,
        })

    for insight in extracted.get("insights", []):
        memories.append({
            "orgId": org_id,
            "scopeType": scope_type,
            "scopeId": scope_id,
            "memoryType": "insight",
            "title": insight["title"],
            "summary": insight["summary"],
            "importance": insight.get("importance", 40),
            "confidence": 0.6,
            "relevanceTags": insight.get("tags", []),
            "source": source_base,
        })

    summary_text = extracted.get("session_summary", "")
    if summary_text:
        participants = extracted.get("participants", [])
        memories.append({
            "orgId": org_id,
            "scopeType": scope_type,
            "scopeId": scope_id,
            "memoryType": "room_summary" if room_id else "session_summary",
            "title": f"Session summary",
            "summary": summary_text,
            "importance": 30,
            "confidence": 0.9,
            "relevanceTags": ["summary"],
            "source": source_base,
            "actorRefs": [
                {"displayName": p.get("name", ""), "role": p.get("contribution", "")}
                for p in participants
            ],
        })

    return memories
