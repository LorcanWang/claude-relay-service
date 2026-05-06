"""
Provider-neutral tool definitions.

Each tool is stored once in a canonical dict and rendered into the format
required by the target provider.  Anthropic uses `input_schema`; OpenAI
wraps tools in `{type: "function", function: {name, description, parameters}}`.

The relay's smart router also translates tools automatically, but having the
definitions ready in the correct format avoids a double-translation round trip
and makes the orchestrator self-documenting about which tools it registers.
"""

from __future__ import annotations

from typing import Any

# ── Canonical tool definitions ───────────────────────────────────────────────
# Stored in Anthropic format (name + description + input_schema) since that's
# the existing internal representation.  OpenAI format is derived from this.

TOOLS: list[dict[str, Any]] = [
    {
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
    },
    {
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
    },
    {
        "name": "app_action",
        "description": (
            "Suggest an action in the Zeon webapp — offer navigation or show a toast. "
            "action='navigate' renders an inline Yes/No card in the chat; the user "
            "chooses whether to open it. action='toast' shows a passive notification and "
            "fires immediately. "
            "IMPORTANT: app_action is a SUPPLEMENT to your answer, never a replacement. "
            "Always write a text response in the same turn that stands on its own even "
            "if the user dismisses the card. Do not end a turn with only app_action "
            "and no text — the user will see a blank chat bubble. "
            "Only request navigate when the user explicitly asks to view/open/show "
            "something; do not navigate after a read-only query or an import that "
            "already returned its result in chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["navigate", "toast"],
                    "description": (
                        "'navigate' to offer a page (user confirms via a Yes/No card); "
                        "'toast' to show an instant notification."
                    ),
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
    },
]


# ── Format renderers ─────────────────────────────────────────────────────────

def to_anthropic(tools: list[dict] | None = None) -> list[dict]:
    """Return tools in Anthropic format (name + description + input_schema).

    This is the canonical format — returns the dicts as-is.
    """
    return list(tools or TOOLS)


def to_openai(tools: list[dict] | None = None) -> list[dict]:
    """Convert canonical tool defs to OpenAI function-calling format.

    OpenAI wraps each tool as:
      {type: "function", function: {name, description, parameters}}
    where `parameters` = our `input_schema`.
    """
    result = []
    for tool in (tools or TOOLS):
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result


def for_provider(provider: str, tools: list[dict] | None = None) -> list[dict]:
    """Render tools in the format required by the given provider."""
    if provider == "anthropic":
        return to_anthropic(tools)
    return to_openai(tools)
