"""
Calls the Anthropic Messages API via the relay (non-streaming).
"""

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8192
REQUEST_TIMEOUT = 120  # seconds

APP_ACTION_TOOL = {
    "name": "app_action",
    "description": (
        "Perform an action in the Zeon webapp — navigate to a page or show a toast notification. "
        "Call this after completing a task to send the user to the right place."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "toast"],
                "description": "'navigate' to go to a page, 'toast' to show a notification",
            },
            "path": {
                "type": "string",
                "description": "URL path for navigate, e.g. '/issues/abc123'",
            },
            "message": {
                "type": "string",
                "description": "Message text for toast",
            },
        },
        "required": ["action"],
    },
}

RUN_COMMAND_TOOL = {
    "name": "run_command",
    "description": (
        "Execute a shell command in a skill's directory. "
        "Use this to run the commands described in SKILL.md files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The skill name (folder name under SKILL_ROOT)",
            },
            "command": {
                "type": "string",
                "description": (
                    "The full command to execute, e.g. "
                    "'python3 ads.py list_campaigns --status ENABLED'"
                ),
            },
        },
        "required": ["skill", "command"],
    },
}


def call_anthropic(
    *,
    base_url: str,
    auth_token: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """
    POST to {base_url}/messages (non-streaming).

    Returns the parsed response dict, or raises on HTTP/network error.
    """
    url = base_url.rstrip("/") + "/messages"

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
        "anthropic-version": "2023-06-01",
        "x-session-id": "orchestrator",   # stable session for relay sticky routing
    }

    logger.debug("Calling Anthropic at %s, model=%s, messages=%d", url, model, len(messages))

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Anthropic API error {resp.status_code}: {resp.text[:500]}"
        )

    return resp.json()


# ── helpers ───────────────────────────────────────────────────────────────────

def extract_text(response: dict) -> str:
    """Concatenate all text blocks from the response content."""
    return "".join(
        block.get("text", "")
        for block in response.get("content", [])
        if block.get("type") == "text"
    )


def extract_tool_uses(response: dict) -> list[dict]:
    """Return all tool_use blocks from the response."""
    return [
        block
        for block in response.get("content", [])
        if block.get("type") == "tool_use"
    ]
