"""
Reads SKILL.md (or skill.md) from SKILL_ROOT/{name}/ and builds the full system prompt.
Also reads agent.json manifests for standardized skill metadata.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SKILL_ROOT = Path(
    os.environ.get("SKILL_ROOT", "/home/hqzn/grantllama-scrape-skill/.claude/skills")
)

# Skills to ignore on this server (loaded from .skillignore in SKILL_ROOT)
_IGNORED_SKILLS: set[str] | None = None

def _load_ignored_skills() -> set[str]:
    global _IGNORED_SKILLS
    if _IGNORED_SKILLS is not None:
        return _IGNORED_SKILLS
    ignore_path = SKILL_ROOT / ".skillignore"
    if ignore_path.exists():
        lines = ignore_path.read_text(encoding="utf-8").splitlines()
        _IGNORED_SKILLS = {l.strip() for l in lines if l.strip() and not l.startswith("#")}
        logger.info("Loaded .skillignore: %s", _IGNORED_SKILLS)
    else:
        _IGNORED_SKILLS = set()
    return _IGNORED_SKILLS

# Filenames to search for, in priority order
_DOC_NAMES = ["SKILL.md", "skill.md", "SKILL.yaml", "skill.yaml", "README.md"]


def load_skill_doc(skill_name: str) -> str | None:
    """Return the SKILL.md content for a skill, or None if not found."""
    # Reject path traversal
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        logger.warning("Rejected unsafe skill name: %r", skill_name)
        return None

    # Check .skillignore
    if skill_name in _load_ignored_skills():
        logger.debug("Skill %s is in .skillignore, skipping", skill_name)
        return None

    skill_dir = SKILL_ROOT / skill_name
    if not skill_dir.is_dir():
        logger.warning("Skill directory not found: %s", skill_dir)
        return None

    for name in _DOC_NAMES:
        doc_path = skill_dir / name
        if doc_path.exists():
            try:
                content = doc_path.read_text(encoding="utf-8")
                logger.debug("Loaded skill doc: %s", doc_path)
                return content
            except Exception as exc:
                logger.warning("Failed to read %s: %s", doc_path, exc)

    logger.warning("No skill doc found in: %s", skill_dir)
    return None


def load_agent_manifest(skill_name: str) -> dict | None:
    """Return parsed agent.json for a skill, or None if not found."""
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        return None
    manifest_path = SKILL_ROOT / skill_name / "agent.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read agent.json for %s: %s", skill_name, exc)
        return None


APP_ACTIONS_BLOCK = (
    "## App Actions\n"
    "You have access to an app_action tool that controls the Zeon webapp directly.\n"
    "Use it proactively after completing tasks:\n"
    "- Call app_action(action='navigate', path='/some/path') to send the user to a relevant page "
    "after creating or updating something (e.g. after creating issue #abc123, navigate to /issues/abc123).\n"
    "- Call app_action(action='toast', message='...') to show a brief success or error notification "
    "without navigating away.\n"
    "Always call app_action as a tool — never describe the action in text alone. "
    "You may chain it after a run_command in the same loop.\n"
    "CRITICAL: app_action supplements your answer, it does not replace it. Every turn must "
    "end with a text response summarizing what you found or did. A turn that ends with only "
    "an app_action call and empty text leaves the chat bubble blank — never do this."
)

SKILL_USAGE_BLOCK = (
    "## Using Skills\n"
    "The Available Skills section below lists what is enabled for this room. Each entry shows "
    "the skill name and a short description. To see the full command list for a skill (its "
    "subcommands, arguments, and examples), call the `describe_skill` tool with the skill name. "
    "Only load full docs when you actually intend to use that skill — the compact index above "
    "is enough for planning.\n"
    "Once you know the command, execute it via `run_command(skill=..., command='python3 X.py ...')`. "
    "Pass the exact Python invocation shown in the skill docs."
)


def _skill_index_line(skill: dict) -> str:
    """Compact one-line entry for the skill index: `- name — description`."""
    name = skill.get("name", "")
    manifest = load_agent_manifest(name) or {}
    desc = (manifest.get("description") or skill.get("description") or "").strip()
    # Truncate overlong descriptions — the index is meant to be scannable.
    if len(desc) > 220:
        desc = desc[:217] + "..."
    if desc:
        return f"- **{name}** — {desc}"
    return f"- **{name}**"


_SENSITIVE_KEY_HINTS = ("token", "secret", "key", "password", "credential", "api_key", "access_token")


def _redact_config_value(key: str, value) -> str:
    """Hide secrets in the skill index. Keep short non-sensitive values visible."""
    if any(hint in key.lower() for hint in _SENSITIVE_KEY_HINTS):
        return "<redacted>"
    text = str(value)
    if len(text) > 80:
        return text[:77] + "..."
    return text


def build_skill_index(enabled_skills: list[dict], skill_configs: dict | None = None) -> str:
    """Compact skill menu. Full docs are fetched lazily via the describe_skill tool."""
    if not enabled_skills:
        return ""
    lines = ["## Available Skills"]
    for skill in enabled_skills:
        lines.append(_skill_index_line(skill))
        name = skill.get("name", "")
        config = (skill_configs or {}).get(name, {})
        if config:
            cfg_preview = ", ".join(
                f"{k}={_redact_config_value(k, v)}" for k, v in config.items()
            )
            lines.append(f"  _config: {cfg_preview}_")
    lines.append("")
    lines.append(
        "Call `describe_skill(name='<skill>')` to see the full command list before using "
        "`run_command` on that skill."
    )
    return "\n".join(lines)


def build_system_prompt(
    base_prompt: str,
    enabled_skills: list[dict],
    org_id: str | None = None,
    user_id: str | None = None,
    in_platform: bool = False,
    skill_configs: dict | None = None,
    room_id: str | None = None,
) -> list[dict]:
    """
    Build the system prompt as a list of Anthropic `text` blocks so we can mark
    the stable prefix with `cache_control` and leave dynamic tail uncached.

    Layout:
      [0] stable core  — base prompt + app actions + skill usage (cache_control)
      [1] skill index  — per-room compact menu (no cache; changes if room skills change)
      [2] dynamic tail — user/room context (no cache; varies per turn)

    The caller attaches cache_control to block 0 before sending.
    """
    # ── Segment 0: stable core ────────────────────────────────────────────
    core_parts = [base_prompt.strip(), APP_ACTIONS_BLOCK, SKILL_USAGE_BLOCK]
    stable_core = "\n\n".join(p for p in core_parts if p)

    # ── Segment 1: skill index (per-room, small) ─────────────────────────
    skill_index = build_skill_index(enabled_skills, skill_configs)

    # ── Segment 2: dynamic tail (user/room context) ──────────────────────
    ctx_lines: list[str] = []
    if org_id or user_id or in_platform or room_id:
        ctx_lines.append("## Current User Context")
        if org_id:
            ctx_lines.append(f"- **org_id**: `{org_id}`")
        if user_id:
            ctx_lines.append(f"- **user_id**: `{user_id}`")
        ctx_lines.append(f"- **in_platform**: `{'true' if in_platform else 'false'}`")
        if room_id:
            ctx_lines.append(f"- **room_id**: `{room_id}`")
        ctx_lines.append(
            "When running skill commands that accept `--org-id`, always pass the org_id above. "
            "Note: LYNX_ORG_ID, LYNX_USER_ID, and skill config values are also injected as "
            "environment variables automatically — skills that read env vars will get them."
        )
        if in_platform:
            ctx_lines.append(
                "The user is inside the platform. Prefer app_action(navigate) to send them to "
                "the relevant page rather than printing full data tables. Always write a clear "
                "text answer summarizing what you found, THEN call app_action."
            )
        if room_id:
            ctx_lines.append(
                "You are in a multi-user meeting room. Messages may be prefixed with "
                "[Username]: to identify the sender. Address users by name when relevant."
            )
    dynamic_tail = "\n".join(ctx_lines)

    blocks: list[dict] = [{"type": "text", "text": stable_core}]
    if skill_index:
        blocks.append({"type": "text", "text": skill_index})
    if dynamic_tail:
        blocks.append({"type": "text", "text": dynamic_tail})
    return blocks
