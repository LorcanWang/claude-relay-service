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


def _sse(data) -> str:
    if data == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_text(text: str, finish_reason: str = "stop") -> AsyncIterator[str]:
    """
    Yield SSE events for a complete text response.
    Chunks the text into ~20-char pieces for a streaming feel.
    """
    CHUNK = 20

    yield _sse({"type": "start"})
    yield _sse({"type": "start-step"})
    yield _sse({"type": "text-start", "id": "0"})

    for i in range(0, max(len(text), 1), CHUNK):
        yield _sse({"type": "text-delta", "id": "0", "delta": text[i : i + CHUNK]})

    yield _sse({"type": "text-end", "id": "0"})
    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish", "finishReason": finish_reason})
    yield _sse("[DONE]")


async def stream_error(message: str) -> AsyncIterator[str]:
    """Yield an error event followed by DONE."""
    yield _sse({"type": "error", "error": message})
    yield _sse("[DONE]")
