"""
Calls the Anthropic Messages API via the relay (non-streaming + streaming).
"""

import gzip
import json
import logging
from typing import Any, AsyncIterator

import aiohttp
import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8192
REQUEST_TIMEOUT = 120  # seconds

APP_ACTION_TOOL = {
    "name": "app_action",
    "description": (
        "Perform an action in the Zeon webapp — navigate to a page or show a toast notification. "
        "Call this after completing a task to send the user to the right place. "
        "IMPORTANT: app_action is a SUPPLEMENT to your answer, never a replacement. "
        "You must always write a text response in the same turn explaining what you found or did. "
        "Do not end the turn with only an app_action and no text — the user will see a blank chat bubble."
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

DESCRIBE_SKILL_TOOL = {
    "name": "describe_skill",
    "description": (
        "Fetch the full documentation for one enabled skill — its subcommand list, "
        "argument schemas, and usage examples. Call this BEFORE using `run_command` on a "
        "skill you haven't used yet in this conversation, so you know the exact invocation. "
        "The skill index in the system prompt only shows the name and one-line description; "
        "this tool returns the full SKILL.md body for planning."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Skill name (must be one of the skills listed under '## Available Skills' "
                    "in the system prompt)."
                ),
            },
        },
        "required": ["name"],
    },
}


def _opus_thinking_config(
    model: str, budget_tokens: int | None, max_tokens: int | None = None
) -> dict | None:
    """Return a `thinking` param dict for Opus models, or None otherwise.

    Anthropic extended thinking: https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking
    Only fires on Opus. Sonnet/Haiku turns stay lean. `budget_tokens` is clamped
    below `max_tokens` (Anthropic requires budget < max) to avoid config errors.
    """
    if not model or not budget_tokens or budget_tokens <= 0:
        return None
    if "opus" not in model.lower():
        return None
    budget = int(budget_tokens)
    if max_tokens and budget >= max_tokens:
        budget = max(512, max_tokens - 512)
    return {"type": "enabled", "budget_tokens": budget}


def call_anthropic(
    *,
    base_url: str,
    auth_token: str,
    system: str | list[dict],
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    """
    POST to {base_url}/messages (non-streaming).

    `system` may be a plain string (legacy callers like the compaction
    summarizer) or an Anthropic-structured list of `{type: "text", text: ...,
    cache_control?: {...}}` blocks (preferred for main chat — enables prompt
    caching on stable prefix).

    `thinking_budget` enables extended thinking but only on Opus models.

    Returns the parsed response dict, or raises on HTTP/network error.
    """
    url = base_url.rstrip("/") + "/v1/messages"

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    thinking_cfg = _opus_thinking_config(model, thinking_budget, max_tokens)
    if thinking_cfg:
        payload["thinking"] = thinking_cfg

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
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        thinking_budget: int | None = None,
    ):
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if tools:
            self._payload["tools"] = tools
        thinking_cfg = _opus_thinking_config(model, thinking_budget, max_tokens)
        if thinking_cfg:
            self._payload["thinking"] = thinking_cfg
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "anthropic-version": "2023-06-01",
            "x-session-id": "orchestrator",
            "Accept-Encoding": "identity",  # prevent gzip on SSE stream
        }

        # Populated during iteration
        self.content: list[dict] = []
        self.stop_reason: str = "end_turn"
        self.tool_uses: list[dict] = []
        # Usage — populated from message_start + final message_delta. Useful to
        # verify prompt caching: non-zero cache_read_input_tokens means cache hit.
        self.usage: dict[str, int] = {}

    def _parse_sse_text(self, text: str) -> None:
        """Parse a full block of SSE text, populating self.content/stop_reason/tool_uses."""
        current_block: dict | None = None
        current_tool_input_json = ""

        for line in text.split("\n"):
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

            if etype == "message_start":
                msg = event.get("message", {}) or {}
                usage = msg.get("usage", {}) or {}
                # Merge — message_start has most fields; message_delta may bump output_tokens.
                for k, v in usage.items():
                    if isinstance(v, int):
                        self.usage[k] = v

            elif etype == "content_block_start":
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
                elif btype == "thinking":
                    # Extended thinking — preserve in content for follow-up turns,
                    # but don't surface to the UI stream.
                    current_block = {"type": "thinking", "thinking": "", "signature": ""}
                    self.content.append(current_block)

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                dtype = delta.get("type", "")
                if dtype == "text_delta" and current_block and current_block["type"] == "text":
                    current_block["text"] += delta.get("text", "")
                elif dtype == "input_json_delta" and current_block and current_block["type"] == "tool_use":
                    current_tool_input_json += delta.get("partial_json", "")
                elif dtype == "thinking_delta" and current_block and current_block["type"] == "thinking":
                    current_block["thinking"] += delta.get("thinking", "")
                elif dtype == "signature_delta" and current_block and current_block["type"] == "thinking":
                    current_block["signature"] += delta.get("signature", "")

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
                usage = event.get("usage", {}) or {}
                for k, v in usage.items():
                    if isinstance(v, int):
                        self.usage[k] = v

    async def __aiter__(self) -> AsyncIterator[str]:
        """Yield text delta strings as they arrive. Populates self.content/stop_reason/tool_uses."""
        # Use aiohttp — httpx corrupts binary data via implicit UTF-8 decoding
        timeout = aiohttp.ClientTimeout(connect=30, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            async with session.post(self._url, json=self._payload, headers=self._headers) as resp:
                if resp.status != 200:
                    body = await resp.read()
                    raise RuntimeError(
                        f"Anthropic API error {resp.status}: {body.decode()[:500]}"
                    )

                content_encoding = resp.headers.get("Content-Encoding", "").lower()
                logger.debug("Stream response content-type: %s encoding: %s",
                             resp.headers.get("Content-Type", ""), content_encoding or "none")

                # ── Peek at first chunk to detect gzip (Cloudflare may lie about Content-Encoding) ──
                first_chunk = await resp.content.readany()
                is_gzip = first_chunk[:2] == b"\x1f\x8b" or "gzip" in content_encoding or "br" in content_encoding

                if is_gzip:
                    # Collect all bytes then decompress (Cloudflare buffers SSE anyway)
                    logger.info("Gzip-compressed SSE, collecting all bytes for decompression")
                    raw_bytes = first_chunk
                    async for chunk in resp.content.iter_any():
                        raw_bytes += chunk
                    try:
                        text = gzip.decompress(raw_bytes).decode("utf-8", errors="replace")
                    except Exception as exc:
                        logger.error("Gzip decompress failed: %s", exc)
                        text = raw_bytes.decode("utf-8", errors="replace")
                    self._parse_sse_text(text)
                    for block in self.content:
                        if block.get("type") == "text":
                            yield block.get("text", "")
                    return

                # ── Plain text SSE — stream line-by-line in real-time ──────────────────────────
                logger.debug("Plain SSE, streaming in real-time")
                current_block: dict | None = None
                current_tool_input_json = ""
                buffer = first_chunk.decode("utf-8", errors="replace")

                async def _lines():
                    nonlocal buffer
                    # Yield any complete lines already in the initial buffer
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        yield line
                    async for chunk in resp.content.iter_any():
                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            yield line
                    # flush remaining
                    if buffer.strip():
                        yield buffer

                async for line in _lines():
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

                    if etype == "message_start":
                        msg = event.get("message", {}) or {}
                        usage = msg.get("usage", {}) or {}
                        for k, v in usage.items():
                            if isinstance(v, int):
                                self.usage[k] = v

                    elif etype == "content_block_start":
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
                        elif btype == "thinking":
                            current_block = {"type": "thinking", "thinking": "", "signature": ""}
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
                        elif dtype == "thinking_delta" and current_block and current_block["type"] == "thinking":
                            current_block["thinking"] += delta.get("thinking", "")
                        elif dtype == "signature_delta" and current_block and current_block["type"] == "thinking":
                            current_block["signature"] += delta.get("signature", "")

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
                        usage = event.get("usage", {}) or {}
                        for k, v in usage.items():
                            if isinstance(v, int):
                                self.usage[k] = v


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
