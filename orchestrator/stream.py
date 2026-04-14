"""
SSE response formatting — matches the AI SDK UI stream protocol that
the browser's useChat hook expects.

Format:
  data: {"type":"start"}
  data: {"type":"start-step"}
  data: {"type":"text-start","id":"0"}
  data: {"type":"text-delta","id":"0","delta":"chunk..."}
  data: {"type":"text-end","id":"0"}
  data: {"type":"finish-step"}
  data: {"type":"finish","finishReason":"stop"}
  data: [DONE]
"""

import json
from typing import AsyncIterator


def sse(data) -> str:
    """Format a single SSE data line."""
    if data == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_agent_switch(from_agent_id: str, to_agent_id: str, reason: str = "") -> str:
    return sse(
        {
            "type": "agent-switch",
            "fromAgentId": from_agent_id,
            "toAgentId": to_agent_id,
            "reason": reason,
        }
    )


def sse_agent_status(agent_id: str, status: str, label: str = "") -> str:
    return sse(
        {
            "type": "agent-status",
            "agentId": agent_id,
            "status": status,
            "label": label,
        }
    )


async def stream_text(
    text: str, actions: list[dict] | None = None, finish_reason: str = "stop"
) -> AsyncIterator[str]:
    """
    Yield SSE events for a complete text response.
    Chunks the text into ~20-char pieces for a streaming feel.
    """
    CHUNK = 20

    yield sse({"type": "start"})
    yield sse({"type": "start-step"})
    yield sse({"type": "text-start", "id": "0"})

    for i in range(0, max(len(text), 1), CHUNK):
        yield sse({"type": "text-delta", "id": "0", "delta": text[i : i + CHUNK]})

    yield sse({"type": "text-end", "id": "0"})

    if actions:
        for action in actions:
            yield sse({"type": "data-action", "data": action})

    yield sse({"type": "finish-step"})
    yield sse({"type": "finish", "finishReason": finish_reason})
    yield sse("[DONE]")


async def stream_error(message: str) -> AsyncIterator[str]:
    """Yield an error event followed by DONE."""
    yield sse({"type": "error", "error": message})
    yield sse("[DONE]")
