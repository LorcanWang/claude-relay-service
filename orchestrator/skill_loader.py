"""
Reads SKILL.md (or skill.md) from SKILL_ROOT/{name}/ and builds the full system prompt.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SKILL_ROOT = Path(
    os.environ.get("SKILL_ROOT", "/home/hqzn/grantllama-scrape-skill/.claude/skills")
)

# Filenames to search for, in priority order
_DOC_NAMES = ["SKILL.md", "skill.md", "SKILL.yaml", "skill.yaml", "README.md"]


def load_skill_doc(skill_name: str) -> str | None:
    """Return the SKILL.md content for a skill, or None if not found."""
    # Reject path traversal
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        logger.warning("Rejected unsafe skill name: %r", skill_name)
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


def build_system_prompt(base_prompt: str, enabled_skills: list[dict]) -> str:
    """
    Combine the base system prompt from Next.js with SKILL.md content
    and tool usage instructions.
    """
    parts = [base_prompt.strip()]

    skill_docs = []
    for skill in enabled_skills:
        name = skill.get("name", "")
        doc = load_skill_doc(name)
        if doc:
            skill_docs.append(f"=== SKILL: {name} ===\n{doc.strip()}\n=== END SKILL ===")
        else:
            # Still include it even without docs so the model knows the skill exists
            desc = skill.get("description", "")
            skill_docs.append(f"=== SKILL: {name} ===\n{desc}\n=== END SKILL ===")

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
