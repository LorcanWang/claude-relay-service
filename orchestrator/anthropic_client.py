"""
Calls the Anthropic Messages API via the relay (non-streaming + streaming).
"""

import json
import logging
from typing import Any, AsyncIterator

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


class AnthropicStream:
    """
    Streams from the Anthropic Messages API.

    Usage:
        stream = AnthropicStream(base_url=..., ...)
        async for text_delta in stream:
            # forward delta to client
            pass

        # After iteration completes:
        stream.stop_reason   # "end_turn" | "tool_use" | ...
        stream.content       # list of content blocks for session persistence
        stream.tool_uses     # list of tool_use blocks (if any)
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth_token: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self._url = base_url.rstrip("/") + "/messages"
        self._payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if tools:
            self._payload["tools"] = tools
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "anthropic-version": "2023-06-01",
            "x-session-id": "orchestrator",
        }

        # Populated during iteration
        self.content: list[dict] = []
        self.stop_reason: str = "end_turn"
        self.tool_uses: list[dict] = []

    async def __aiter__(self) -> AsyncIterator[str]:
        """Yield text delta strings as they arrive. Populates self.content/stop_reason/tool_uses."""
        current_block: dict | None = None
        current_tool_input_json = ""
        full_response = ""  # accumulate for JSON fallback

        # Connect timeout is short; read timeout is long for streaming
        stream_timeout = httpx.Timeout(connect=30, read=300, write=30, pool=30)
        async with httpx.AsyncClient(timeout=stream_timeout) as client:
            async with client.stream("POST", self._url, json=self._payload, headers=self._headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"Anthropic API error {resp.status_code}: {body.decode()[:500]}"
                    )

                content_type = resp.headers.get("content-type", "")
                logger.info("Stream response content-type: %s", content_type)

                buffer = ""
                async for raw_chunk in resp.aiter_text():
                    buffer += raw_chunk
                    full_response += raw_chunk

                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()

                        if not line or line.startswith("event:"):
                            continue
                        if not line.startswith("data: "):
                            continue

                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue

                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", "")

                        if etype == "content_block_start":
                            block = event.get("content_block", {})
                            btype = block.get("type", "")
                            if btype == "text":
                                current_block = {"type": "text", "text": ""}
                                self.content.append(current_block)
                            elif btype == "tool_use":
                                current_block = {
                                    "type": "tool_use",
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "input": {},
                                }
                                current_tool_input_json = ""
                                self.content.append(current_block)

                        elif etype == "content_block_delta":
                            delta = event.get("delta", {})
                            dtype = delta.get("type", "")
                            if dtype == "text_delta" and current_block and current_block["type"] == "text":
                                text = delta.get("text", "")
                                current_block["text"] += text
                                yield text
                            elif dtype == "input_json_delta" and current_block and current_block["type"] == "tool_use":
                                current_tool_input_json += delta.get("partial_json", "")

                        elif etype == "content_block_stop":
                            if current_block and current_block["type"] == "tool_use":
                                try:
                                    current_block["input"] = json.loads(current_tool_input_json) if current_tool_input_json else {}
                                except json.JSONDecodeError:
                                    current_block["input"] = {}
                                self.tool_uses.append(current_block)
                            current_block = None
                            current_tool_input_json = ""

                        elif etype == "message_delta":
                            delta = event.get("delta", {})
                            if "stop_reason" in delta:
                                self.stop_reason = delta["stop_reason"]

        # ── Fallback: relay returned non-streaming JSON ──────────────────────
        if not self.content and full_response.strip():
            logger.warning(
                "No SSE content blocks parsed. Attempting JSON fallback. "
                "Response preview: %s", full_response[:300]
            )
            try:
                response = json.loads(full_response.strip())
                self.stop_reason = response.get("stop_reason", "end_turn")
                for block in response.get("content", []):
                    btype = block.get("type", "")
                    if btype == "text":
                        self.content.append(block)
                        yield block.get("text", "")
                    elif btype == "tool_use":
                        self.content.append(block)
                        self.tool_uses.append(block)
            except json.JSONDecodeError:
                logger.error("JSON fallback also failed. Raw response: %s", full_response[:500])


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
