"""
OpenAI-compatible streaming client for the relay's /openai/v1/chat/completions.

Sends requests in OpenAI ChatCompletion format and normalizes the response
into the same interface as AnthropicStream (content blocks, stop_reason,
tool_uses, usage) so the orchestrator's tool-use loop works unchanged.

The relay's smart router auto-detects the provider from the model name
(gpt-* → OpenAI, gemini-* → Gemini, claude-* → Anthropic) and handles
the actual provider translation.  This client just speaks OpenAI wire format.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

import aiohttp

from provider_client import (
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_STOP,
    STOP_REASON_TOOL_USE,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 16000

# ── Message format conversion ────────────────────────────────────────────────

def anthropic_messages_to_openai(
    system: str | list[dict],
    messages: list[dict],
) -> list[dict]:
    """
    Convert Anthropic-shaped session messages to OpenAI chat messages.

    The orchestrator stores messages in Anthropic format internally.  This
    converts them for the OpenAI-compatible relay endpoint:

    - system blocks → single system message (text concatenated)
    - user messages with tool_result content → one tool message per result
    - assistant messages with tool_use content → assistant + tool_calls
    - plain text messages → passed through
    """
    openai_msgs: list[dict] = []

    # System prompt
    if system:
        if isinstance(system, str):
            sys_text = system
        else:
            sys_text = "\n\n".join(
                b.get("text", "") for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if sys_text.strip():
            openai_msgs.append({"role": "system", "content": sys_text})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            openai_msgs.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            openai_msgs.append({"role": role, "content": str(content)})
            continue

        # Content is a list of blocks (Anthropic format)
        if role == "assistant":
            text_parts = []
            tool_calls = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
                # thinking blocks are dropped — not supported by OpenAI

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            combined_text = "".join(text_parts)
            if combined_text:
                assistant_msg["content"] = combined_text
            else:
                assistant_msg["content"] = None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            openai_msgs.append(assistant_msg)

        elif role == "user":
            # Check if this is tool results
            has_tool_results = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
            if has_tool_results:
                for block in content:
                    if block.get("type") == "tool_result":
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            result_content = "\n".join(
                                b.get("text", "") for b in result_content
                                if isinstance(b, dict)
                            )
                        openai_msgs.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": str(result_content),
                        })
            else:
                # Regular user message with content blocks
                text = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                openai_msgs.append({"role": "user", "content": text or ""})

    return openai_msgs


def _normalize_stop_reason(finish_reason: str | None) -> str:
    """Map OpenAI finish_reason to normalized stop reason."""
    if not finish_reason:
        return STOP_REASON_STOP
    mapping = {
        "stop": STOP_REASON_STOP,
        "tool_calls": STOP_REASON_TOOL_USE,
        "function_call": STOP_REASON_TOOL_USE,
        "length": STOP_REASON_MAX_TOKENS,
    }
    return mapping.get(finish_reason, STOP_REASON_STOP)


# ── Streaming client ─────────────────────────────────────────────────────────

class OpenAIStream:
    """
    Streams from the relay's OpenAI-compatible /v1/chat/completions endpoint.

    Conforms to the same interface as AnthropicStream:
    - Yields text delta strings
    - Populates self.content, self.stop_reason, self.tool_uses, self.usage
    - self.content is in Anthropic block format (for session persistence)
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth_token: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        **_kwargs,
    ):
        self._url = base_url.rstrip("/") + "/v1/chat/completions"
        openai_messages = anthropic_messages_to_openai(system, messages)

        self._payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": openai_messages,
            "stream": True,
        }
        if tools:
            self._payload["tools"] = tools

        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "x-session-id": "orchestrator",
        }

        self.content: list[dict] = []
        self.stop_reason: str = STOP_REASON_STOP
        self.tool_uses: list[dict] = []
        self.usage: dict[str, int] = {}

    async def __aiter__(self) -> AsyncIterator[str]:
        """Yield text deltas. Populates content/stop_reason/tool_uses/usage."""
        timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
        logger.info(
            "OpenAI stream opening: model=%s messages=%d url=%s",
            self._payload.get("model", "?"),
            len(self._payload.get("messages", [])),
            self._url[:80],
        )

        current_text = ""
        # tool_calls accumulator: index → {id, name, arguments_json}
        tool_calls_acc: dict[int, dict] = {}
        finish_reason: str | None = None

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self._url, json=self._payload, headers=self._headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.read()
                    raise RuntimeError(
                        f"OpenAI API error {resp.status}: {body.decode()[:500]}"
                    )
                logger.info("OpenAI stream connected: status=%d", resp.status)

                buffer = ""
                async for chunk in resp.content.iter_any():
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line or line.startswith(":"):
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

                        # Usage (sent with stream_options or in final chunk)
                        if "usage" in event and event["usage"]:
                            u = event["usage"]
                            if "prompt_tokens" in u:
                                self.usage["input_tokens"] = u["prompt_tokens"]
                            if "completion_tokens" in u:
                                self.usage["output_tokens"] = u["completion_tokens"]

                        for choice in event.get("choices", []):
                            delta = choice.get("delta", {})
                            fr = choice.get("finish_reason")
                            if fr:
                                finish_reason = fr

                            # Text content
                            text_delta = delta.get("content")
                            if text_delta:
                                current_text += text_delta
                                yield text_delta

                            # Tool calls (streamed incrementally)
                            for tc in delta.get("tool_calls", []):
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {
                                        "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                                        "name": "",
                                        "arguments": "",
                                    }
                                acc = tool_calls_acc[idx]
                                if tc.get("id"):
                                    acc["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    acc["name"] = fn["name"]
                                if fn.get("arguments"):
                                    acc["arguments"] += fn["arguments"]

        # ── Build Anthropic-format content blocks for session persistence ────
        if current_text:
            self.content.append({"type": "text", "text": current_text})

        for idx in sorted(tool_calls_acc.keys()):
            acc = tool_calls_acc[idx]
            try:
                parsed_input = json.loads(acc["arguments"]) if acc["arguments"] else {}
            except json.JSONDecodeError:
                parsed_input = {}
            tool_block = {
                "type": "tool_use",
                "id": acc["id"],
                "name": acc["name"],
                "input": parsed_input,
            }
            self.content.append(tool_block)
            self.tool_uses.append(tool_block)

        self.stop_reason = _normalize_stop_reason(finish_reason)
        logger.info(
            "OpenAI stream done: stop=%s text_len=%d tool_calls=%d",
            self.stop_reason, len(current_text), len(self.tool_uses),
        )


# ── Non-streaming call ───────────────────────────────────────────────────────

def call_openai_compat(
    *,
    base_url: str,
    auth_token: str,
    system: str | list[dict],
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    **_kwargs,
) -> dict[str, Any]:
    """Non-streaming call via the relay's OpenAI-compatible endpoint.

    Returns a dict shaped like an Anthropic response for compatibility with
    existing callers (e.g. session compaction).
    """
    import httpx

    url = base_url.rstrip("/") + "/v1/chat/completions"
    openai_messages = anthropic_messages_to_openai(system, messages)

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": openai_messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
        "x-session-id": "orchestrator",
    }

    with httpx.Client(timeout=120) as client:
        resp = client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()

    # Convert OpenAI response → Anthropic-shaped response
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content_blocks: list[dict] = []

    if msg.get("content"):
        content_blocks.append({"type": "text", "text": msg["content"]})

    for tc in msg.get("tool_calls", []):
        fn = tc.get("function", {})
        try:
            inp = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            inp = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": inp,
        })

    return {
        "content": content_blocks,
        "stop_reason": _normalize_stop_reason(choice.get("finish_reason")),
        "usage": {
            "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
        },
    }
