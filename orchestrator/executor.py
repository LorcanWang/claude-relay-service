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
STDERR_CAP = 1_000_000


def execute_command(
    skill_name: str,
    command: str,
    enabled_skill_names: list[str],
    context: dict | None = None,
) -> dict:
    """
    Run `command` in SKILL_ROOT/{skill_name}/.

    context (optional): dict with org_id, user_id, session_id, skill_configs, in_platform.
    These are injected as LYNX_* env vars so skills have a standard way to access caller context.

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

    # ── build env with standard LYNX_* context vars ──────────────────────────
    ctx = context or {}
    env = {**os.environ, "SKILL_DIR": str(skill_dir)}

    # Prepend skill repo venv to PATH so python3/pip3 resolve correctly
    venv_bin = SKILL_ROOT.parent.parent / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(venv_bin.parent)

    # Standard context vars — every skill gets these
    if ctx.get("org_id"):
        env["LYNX_ORG_ID"] = str(ctx["org_id"])
    if ctx.get("user_id"):
        env["LYNX_USER_ID"] = str(ctx["user_id"])
    if ctx.get("session_id"):
        env["LYNX_SESSION_ID"] = str(ctx["session_id"])
    env["LYNX_AGENT_ID"] = skill_name
    env["LYNX_IN_PLATFORM"] = "true" if ctx.get("in_platform") else "false"
    if ctx.get("room_id"):
        env["LYNX_ROOM_ID"] = str(ctx["room_id"])

    # Per-skill config values as LYNX_CONFIG_* env vars
    skill_configs = ctx.get("skill_configs") or {}
    skill_config = skill_configs.get(skill_name) or {}
    for key, value in skill_config.items():
        env_key = "LYNX_CONFIG_" + key.upper().replace("-", "_")
        env[env_key] = str(value)
    if skill_config:
        env["LYNX_CONFIG_JSON"] = json.dumps(skill_config, ensure_ascii=False, default=str)

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
        logger.warning(
            "Command failed (exit %s): %s\nSTDERR:\n%s\nSTDOUT:\n%s",
            result.returncode,
            command,
            result.stderr.rstrip() or "(empty)",
            result.stdout.rstrip() or "(empty)",
        )
        return {
            "ok": False,
            "error": f"Exit {result.returncode}",
            "stderr": stderr.strip() or None,
            "stdout": stdout.strip() or None,
        }

    if not stdout.strip():
        return {"ok": True, "data": None, "stderr": stderr.strip() or None}

    try:
        parsed = json.loads(stdout)
        result = {"ok": True, "data": parsed}
        # Extract agentNote from skill's standard envelope if present
        if isinstance(parsed, dict) and parsed.get("agentNote"):
            result["agentNote"] = parsed["agentNote"]
        return result
    except json.JSONDecodeError:
        return {"ok": True, "data": stdout.strip()}
