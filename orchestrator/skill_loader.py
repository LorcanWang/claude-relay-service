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


def build_system_prompt(
    base_prompt: str,
    enabled_skills: list[dict],
    org_id: str | None = None,
    user_id: str | None = None,
    in_platform: bool = False,
    skill_configs: dict | None = None,
    room_id: str | None = None,
) -> str:
    """
    Combine the base system prompt from Next.js with SKILL.md content
    and tool usage instructions.
    """
    parts = [base_prompt.strip()]

    if org_id or user_id or in_platform:
        ctx_lines = ["## Current User Context"]
        if org_id:
            ctx_lines.append(f"- **org_id**: `{org_id}`")
        if user_id:
            ctx_lines.append(f"- **user_id**: `{user_id}`")
        ctx_lines.append(f"- **in_platform**: `{'true' if in_platform else 'false'}`")
        ctx_lines.append(
            "When running skill commands that accept `--org-id`, always pass the org_id above. "
            "Note: LYNX_ORG_ID, LYNX_USER_ID, and skill config values are also injected as "
            "environment variables automatically — skills that read env vars will get them."
        )
        if in_platform:
            ctx_lines.append(
                "The user is inside the platform. Prefer using app_action(navigate) to send them "
                "to the relevant page rather than printing full data tables. "
                "Give a brief answer and navigate."
            )
        if room_id:
            ctx_lines.append(f"- **room_id**: `{room_id}`")
            ctx_lines.append(
                "You are in a multi-user meeting room. Messages may be prefixed with "
                "[Username]: to identify the sender. Address users by name when relevant."
            )
        parts.append("\n".join(ctx_lines))

    skill_docs = []
    for skill in enabled_skills:
        name = skill.get("name", "")
        doc = load_skill_doc(name)
        config = (skill_configs or {}).get(name, {})
        config_block = ""
        if config:
            config_lines = "\n".join(f"  {k}: {v}" for k, v in config.items())
            config_block = f"\n## Skill Config\n{config_lines}"
        if doc:
            skill_docs.append(f"=== SKILL: {name} ===\n{doc.strip()}{config_block}\n=== END SKILL ===")
        else:
            desc = skill.get("description", "")
            skill_docs.append(f"=== SKILL: {name} ===\n{desc}{config_block}\n=== END SKILL ===")

    if skill_docs:
        parts.append("\n\n".join(skill_docs))
        parts.append(
            "When the user's request matches a skill, execute the appropriate command "
            "described in the skill documentation above using the run_command tool. "
            "Read the skill's Commands section to determine the correct command and arguments. "
            "Always run the command with the exact Python invocation shown (e.g. python3 ads.py ...)."
        )

    parts.append(
        "## App Actions\n"
        "You have access to an app_action tool that controls the Zeon webapp directly.\n"
        "Use it proactively after completing tasks:\n"
        "- Call app_action(action='navigate', path='/some/path') to send the user to a relevant page "
        "after creating or updating something (e.g. after creating issue #abc123, navigate to /issues/abc123).\n"
        "- Call app_action(action='toast', message='...') to show a brief success or error notification "
        "without navigating away.\n"
        "Always call app_action as a tool — never describe the action in text alone. "
        "You may chain it after a run_command in the same loop."
    )

    return "\n\n".join(parts)
