"""
Provider abstraction layer.

Defines a common interface for LLM providers so the orchestrator's tool-use
loop works identically regardless of whether the backend is Anthropic, OpenAI,
or Gemini.  The relay handles format translation for the wire protocol; this
module normalizes the *orchestrator-internal* representation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


# ── Normalized types ─────────────────────────────────────────────────────────

STOP_REASON_STOP = "stop"
STOP_REASON_TOOL_USE = "tool_use"
STOP_REASON_MAX_TOKENS = "max_tokens"
STOP_REASON_PAUSE_TURN = "pause_turn"


@dataclass
class ToolCall:
    """A single tool invocation extracted from the model response."""
    id: str
    name: str
    input: dict


@dataclass
class ProviderTurn:
    """
    Normalized result of one LLM turn (streaming or non-streaming).

    `content` holds Anthropic-shaped content blocks — the canonical internal
    format.  OpenAI responses are converted into this shape so the rest of
    the orchestrator (session persistence, tool execution, compaction) doesn't
    need to branch.
    """
    content: list[dict] = field(default_factory=list)
    stop_reason: str = STOP_REASON_STOP
    tool_uses: list[dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class ProviderStream(Protocol):
    """Protocol that both AnthropicStream and OpenAIStream implement."""

    content: list[dict]
    stop_reason: str
    tool_uses: list[dict]
    usage: dict[str, int]

    def __aiter__(self) -> AsyncIterator[str]: ...


# ── Provider detection ───────────────────────────────────────────────────────

_CLAUDE_RE = re.compile(r"^claude-", re.IGNORECASE)
_GPT_RE = re.compile(r"^(gpt-|o[1-9]|codex-)", re.IGNORECASE)
_GEMINI_RE = re.compile(r"^gemini-", re.IGNORECASE)


def detect_provider(model: str) -> str:
    """
    Detect the provider from a model name.

    Returns "anthropic", "openai", or "gemini".  Defaults to "anthropic"
    for unknown model strings (preserves backward compat).
    """
    if not model:
        return "anthropic"
    if _CLAUDE_RE.match(model):
        return "anthropic"
    if _GPT_RE.match(model):
        return "openai"
    if _GEMINI_RE.match(model):
        return "gemini"
    return "anthropic"


def is_anthropic_native(provider: str) -> bool:
    """True when the provider should use the Anthropic Messages API directly."""
    return provider == "anthropic"


# ── Tool result helpers ──────────────────────────────────────────────────────

def make_tool_result(tool_id: str, content: str | list[dict]) -> dict:
    """
    Build a tool_result block in Anthropic format.

    The OpenAI client converts this into OpenAI tool message format before
    sending to the relay; the Anthropic client uses it as-is.
    """
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
    }
