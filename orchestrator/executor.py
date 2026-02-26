"""
Execute shell commands inside a skill's directory.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SKILL_ROOT = Path(
    os.environ.get("SKILL_ROOT", "/home/hqzn/grantllama-scrape-skill/.claude/skills")
)
TIMEOUT = int(os.environ.get("SKILL_TIMEOUT", "60"))
STDOUT_CAP = 1_000_000   # 1 MB
STDERR_CAP = 100_000


def execute_command(skill_name: str, command: str, enabled_skill_names: list[str]) -> dict:
    """
    Run `command` in SKILL_ROOT/{skill_name}/.

    Returns {"ok": True, "data": ...} or {"ok": False, "error": ..., "stderr": ...}.
    """
    # ── security checks ───────────────────────────────────────────────────────
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        return {"ok": False, "error": f"Invalid skill name: {skill_name!r}"}

    if skill_name not in enabled_skill_names:
        return {"ok": False, "error": f"Skill '{skill_name}' is not in enabledSkills"}

    skill_dir = SKILL_ROOT / skill_name
    if not skill_dir.is_dir():
        return {"ok": False, "error": f"Skill directory not found: {skill_dir}"}

    logger.info("Executing in %s: %s", skill_dir, command)

    # ── run ───────────────────────────────────────────────────────────────────
    env = {**os.environ, "SKILL_DIR": str(skill_dir)}

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(skill_dir),
            timeout=TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out after {TIMEOUT}s"}
    except Exception as exc:
        return {"ok": False, "error": f"Execution error: {exc}"}

    stdout = result.stdout[:STDOUT_CAP]
    stderr = result.stderr[:STDERR_CAP]

    if result.returncode != 0:
        return {
            "ok": False,
            "error": f"Exit {result.returncode}",
            "stderr": stderr.strip() or None,
            "stdout": stdout.strip() or None,
        }

    if not stdout.strip():
        return {"ok": True, "data": None, "stderr": stderr.strip() or None}

    try:
        return {"ok": True, "data": json.loads(stdout)}
    except json.JSONDecodeError:
        return {"ok": True, "data": stdout.strip()}
