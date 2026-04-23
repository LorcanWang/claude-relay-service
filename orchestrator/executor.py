"""
Execute skill commands inside a skill's directory — sandboxed.

Hardening (post-incident 2026-04-20):
  A model-invoked tool call ran `brew install poppler` via shell=True and the
  grandchild process escaped the 60s timeout and kept going for minutes. Two
  fixes here, both required:

    1. NO SHELL. We parse the command via shlex into an argv list and launch
       with Popen(argv, shell=False). Eliminates every shell-metachar class
       (pipes, redirects, command substitution, env assignment, backticks).
    2. ALLOWLIST. argv[0] must be python/python3; argv[1] must resolve to a
       .py file inside the skill's own directory. Anything else is refused
       before a subprocess is spawned. The model can't reach for brew/apt/
       pip/sudo/rm/curl — there is no interpreter that accepts those words.
    3. PROCESS GROUP KILL. Popen(start_new_session=True) puts the child in
       its own session. On timeout we SIGTERM the group, wait 5s, then
       SIGKILL. Grandchildren die with the parent instead of leaking.
    4. RLIMITs on Linux (best-effort on macOS). Caps open files, child
       processes, CPU-seconds, and single-file write size so a runaway
       skill can't exhaust the VPS.

Logs ALWAYS record: skill, argv[0]+script+sanitized-args, cwd, user/session
ids, refusal reason when rejected. Signed URLs in LYNX_ATTACHMENTS_JSON are
NEVER logged — they're env vars only.
"""

import json
import logging
import os
import re
import resource
import shlex
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SKILL_ROOT = Path(
    os.environ.get("SKILL_ROOT", "/home/hqzn/grantllama-scrape-skill/.claude/skills")
)
TIMEOUT = int(os.environ.get("SKILL_TIMEOUT", "60"))
STDOUT_CAP = 1_000_000   # 1 MB
STDERR_CAP = 1_000_000

# Allowed interpreters for skill argv[0]. Extendable later if a skill genuinely
# needs node/bun/etc., but then each addition is a deliberate policy change —
# not something the model can reach for at runtime.
_ALLOWED_INTERPRETERS = {"python", "python3"}

# Any of these characters appearing in an argv element (after shlex parsing)
# is rejected. With Popen(shell=False, args=[...]) the kernel passes argv as
# a NUL-terminated string array — there is NO shell to interpret &, ;, |, $,
# `, <, > as metacharacters. They're literal data. The earlier regex blocked
# all of them as defense-in-depth and produced false-positive refusals on
# legitimate user data (real case: brand tagline "Fast Printing & Shipping"
# rejected on instagram-marketing.creative — see commit log).
#
# What actually matters with shell=False:
#   \x00 (NUL): truncates the C string the kernel sees; argv element gets
#               silently chopped → real injection vector. Keep blocked.
#   \n / \r:    don't break execution, but argv elements with newlines look
#               like multiple commands in audit logs / grep. Keep blocked
#               for log hygiene.
#
# Re-add a meta block ONLY if we ever spawn a subshell (`shell=True` or
# pipe through `bash -c`). For the current Popen(shell=False, [...]) path,
# this narrower set is correct.
_FORBIDDEN_ARG_CHARS = re.compile(r"[\x00\n\r]")

# Env-var assignments that may prefix the command. The skill invocation
# convention (taught in each SKILL.md) is:
#     SKILL_ARGS_JSON='{...}' python3 run.py
# Under shell=True this would set the var for the command; under shell=False
# we must split them off argv, allowlist the name, and inject via env dict.
# Only names in this set are accepted — the model can't clobber LYNX_ORG_ID
# or other trusted vars, which would enable org impersonation.
_ALLOWED_ENV_PREFIX_KEYS = {"SKILL_ARGS_JSON"}
_ENV_ASSIGN_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")

# Graceful-shutdown window between SIGTERM and SIGKILL on the process group.
_TERM_KILL_GRACE_SECONDS = 5


class RefusedCommand(Exception):
    """Raised when a command fails the allowlist. Message is user-safe (no
    secrets) so it can be bubbled back to the model as an error envelope."""


def _check_argv(argv: list[str], skill_dir: Path) -> tuple[str, Path, list[str], dict[str, str]]:
    """Validate a parsed argv against the allowlist.

    Accepts an optional prefix of env-var assignments (KEY=value) before the
    interpreter — only KEYs in `_ALLOWED_ENV_PREFIX_KEYS` are honored. The
    values are stripped off argv and returned separately to be injected into
    the subprocess env dict.

    Returns (resolved_interpreter, resolved_script_path, args_tail, prefix_env)
    or raises RefusedCommand with a precise reason. All returned paths are
    absolute and guaranteed to sit inside `skill_dir` after resolution.
    """
    if not argv:
        raise RefusedCommand("empty_command")

    # ── parse leading KEY=value pairs ────────────────────────────────────────
    prefix_env: dict[str, str] = {}
    idx = 0
    while idx < len(argv):
        m = _ENV_ASSIGN_RE.match(argv[idx])
        if not m:
            break
        key, value = m.group(1), m.group(2)
        if key not in _ALLOWED_ENV_PREFIX_KEYS:
            raise RefusedCommand(
                f"env_var_not_allowed: {key} (allowed: {sorted(_ALLOWED_ENV_PREFIX_KEYS)})"
            )
        prefix_env[key] = value
        idx += 1
    argv = argv[idx:]
    if not argv:
        raise RefusedCommand("no_command_after_env_prefix")

    interp = os.path.basename(argv[0])
    if interp not in _ALLOWED_INTERPRETERS:
        raise RefusedCommand(
            f"interpreter_not_allowed: {interp!r} (allowed: {sorted(_ALLOWED_INTERPRETERS)})"
        )

    if len(argv) < 2:
        raise RefusedCommand("missing_script")

    raw_script = argv[1]
    if not raw_script.endswith(".py"):
        raise RefusedCommand(f"script_not_python: {raw_script!r}")

    # Path-traversal defense: resolve the script relative to the skill dir
    # and verify the result is still inside it. Catches ../.., absolute
    # paths pointing elsewhere, and symlinks that escape.
    script_abs = (skill_dir / raw_script).resolve()
    skill_dir_abs = skill_dir.resolve()
    try:
        script_abs.relative_to(skill_dir_abs)
    except ValueError:
        raise RefusedCommand(
            f"script_outside_skill_dir: {raw_script!r}"
        )
    if not script_abs.is_file():
        raise RefusedCommand(f"script_not_found: {raw_script!r}")

    tail = argv[2:]
    for i, a in enumerate(tail):
        if _FORBIDDEN_ARG_CHARS.search(a):
            raise RefusedCommand(f"forbidden_char_in_arg[{i}]: {a[:60]!r}")

    # Resolve interpreter via PATH lookup so we spawn a real absolute path
    # and the model can't trick us with a custom PATH entry shadowing python.
    # We trust the environment PATH here — the executor itself is trusted,
    # only the ARGUMENTS are untrusted.
    return interp, script_abs, tail, prefix_env


def _apply_rlimits():
    """Best-effort resource caps on the child. Called inside preexec_fn so it
    runs after fork but before exec. macOS silently ignores some RLIMITs; we
    don't raise on failures."""
    # Max child-process count. Stops a runaway fork bomb. 4096 is standard
    # per-user cap on Linux; on macOS this counts ALL user processes (not
    # per-session) so keep it generous to avoid false-positive EAGAINs on
    # machines that already have many Python processes running.
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (4096, 4096))
    except (ValueError, OSError, AttributeError):
        pass
    # Max open file descriptors. Skills that legitimately open many files
    # should bump this via env — 1024 is ample for most.
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    except (ValueError, OSError, AttributeError):
        pass
    # Max CPU-seconds. Task worker passes its own timeout_seconds for long-
    # running tasks; this is belt-and-suspenders for the sync path.
    try:
        # Align with the task timeout ceiling to avoid tripping it early
        # on long-running invocations. The wall-clock timeout still applies.
        resource.setrlimit(resource.RLIMIT_CPU, (3600, 3600))
    except (ValueError, OSError, AttributeError):
        pass
    # Max single-file write size. Prevents a skill from writing a multi-GB
    # artifact and filling disk.
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass


def _kill_process_group(proc: subprocess.Popen):
    """TERM→grace→KILL the entire process group. Handles the case where the
    process already exited (ProcessLookupError) between our last poll and
    the signal call."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        logger.warning("killpg SIGTERM failed for pgid=%s: %s", pgid, exc)
    try:
        proc.wait(timeout=_TERM_KILL_GRACE_SECONDS)
        return  # exited cleanly on TERM
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        logger.error("process group %s survived SIGKILL — this should not happen", pgid)


def _audit_log(kind: str, *, skill_name: str, argv_preview: str, cwd: str,
               reason: str = "", ctx: dict | None = None):
    """Structured log line for every refused/timed-out/failed command.
    Never include signed URLs or LYNX_ATTACHMENTS_JSON content — those ride
    in env vars and must not hit disk."""
    ctx = ctx or {}
    logger.warning(
        "executor.%s skill=%s argv=%s cwd=%s reason=%s user=%s session=%s room=%s",
        kind,
        skill_name,
        argv_preview,
        cwd,
        reason or "-",
        ctx.get("user_id", "-"),
        ctx.get("session_id", "-"),
        ctx.get("room_id", "-"),
    )


def execute_command(
    skill_name: str,
    command: str,
    enabled_skill_names: list[str],
    context: dict | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    """
    Run a python3 script from `SKILL_ROOT/{skill_name}/` under the sandbox.

    Only the pattern `python[3] <script>.py [args]` is accepted; `<script>.py`
    must resolve to a file inside the skill's own directory (no traversal,
    no symlink escape). Shell metachars in args are rejected. The child is
    launched in its own session so a timeout kills the whole process group.

    context (optional): dict with org_id, user_id, session_id, skill_configs,
    in_platform, room_id, attachments. Injected as LYNX_* env vars.

    timeout_seconds (optional): wall-clock cap. Defaults to SKILL_TIMEOUT
    (60s). Task worker passes its longer budget explicitly.

    Returns {"ok": True, "data": ...} | {"ok": False, "error": ..., ...}.
    """
    # ── skill-name safety ────────────────────────────────────────────────────
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        return {"ok": False, "error": f"Invalid skill name: {skill_name!r}"}
    if skill_name not in enabled_skill_names:
        return {"ok": False, "error": f"Skill '{skill_name}' is not in enabledSkills"}

    skill_dir = SKILL_ROOT / skill_name
    if not skill_dir.is_dir():
        return {"ok": False, "error": f"Skill directory not found: {skill_dir}"}

    # ── parse + allowlist check ──────────────────────────────────────────────
    try:
        argv = shlex.split(command or "")
    except ValueError as exc:
        _audit_log("refused", skill_name=skill_name, argv_preview=command[:120],
                   cwd=str(skill_dir), reason=f"shlex_parse_error: {exc}",
                   ctx=context)
        return {"ok": False, "error": f"refused_command: shlex_parse_error: {exc}"}

    try:
        interp, script_abs, tail, prefix_env = _check_argv(argv, skill_dir)
    except RefusedCommand as exc:
        _audit_log("refused", skill_name=skill_name, argv_preview=" ".join(argv[:4]),
                   cwd=str(skill_dir), reason=str(exc), ctx=context)
        return {"ok": False, "error": f"refused_command: {exc}"}

    argv_final = [interp, str(script_abs), *tail]

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

    # Phase 10: attachments uploaded with the current user turn. List of
    # {id, name, mimeType, url, sizeBytes}. Skills that care read + download
    # via the signed URL to a tmpdir. URLs are NEVER logged.
    attachments = ctx.get("attachments") or []
    if attachments:
        env["LYNX_ATTACHMENTS_JSON"] = json.dumps(
            attachments, ensure_ascii=False, default=str,
        )

    # Model-supplied env-var prefix (e.g. SKILL_ARGS_JSON='...') — injected
    # LAST so it overrides anything above if the skill relies on it.
    for k, v in prefix_env.items():
        env[k] = v

    effective_timeout = timeout_seconds if timeout_seconds is not None else TIMEOUT
    argv_preview = f"{interp} {script_abs.name}"
    if tail:
        argv_preview += f" ({len(tail)} args)"
    logger.info("Executing in %s: %s", skill_dir, argv_preview)

    # ── launch in its own process group ──────────────────────────────────────
    try:
        proc = subprocess.Popen(
            argv_final,
            shell=False,
            cwd=str(skill_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # os.setsid → its own session + pgroup
            preexec_fn=_apply_rlimits if sys.platform != "win32" else None,
        )
    except Exception as exc:
        _audit_log("launch_failed", skill_name=skill_name,
                   argv_preview=argv_preview, cwd=str(skill_dir),
                   reason=str(exc), ctx=context)
        return {"ok": False, "error": f"Execution error: {exc}"}

    try:
        stdout, stderr = proc.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # Drain any buffered output after the kill.
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except Exception:
            stdout, stderr = "", ""
        _audit_log("timeout", skill_name=skill_name, argv_preview=argv_preview,
                   cwd=str(skill_dir),
                   reason=f"wall_clock_{effective_timeout}s", ctx=context)
        return {"ok": False, "error": f"Command timed out after {effective_timeout}s"}
    except Exception as exc:
        _kill_process_group(proc)
        _audit_log("comm_error", skill_name=skill_name,
                   argv_preview=argv_preview, cwd=str(skill_dir),
                   reason=str(exc), ctx=context)
        return {"ok": False, "error": f"Execution error: {exc}"}

    stdout = (stdout or "")[:STDOUT_CAP]
    stderr = (stderr or "")[:STDERR_CAP]

    if proc.returncode != 0:
        logger.warning(
            "Command failed (exit %s): %s\nSTDERR:\n%s\nSTDOUT:\n%s",
            proc.returncode,
            argv_preview,
            stderr.rstrip() or "(empty)",
            stdout.rstrip() or "(empty)",
        )
        # Synthesize an informative `error` string. Callers (main.py and the
        # room-message formatter) read `error` first; a bare "Exit 1" makes
        # debugging impossible, so fold a tail of stderr (or stdout) into it.
        # Keep stderr/stdout fields too — they remain the structured source.
        tail_src = stderr.strip() or stdout.strip()
        if tail_src:
            tail_lines = [l for l in tail_src.splitlines() if l.strip()][-5:]
            snippet = "\n".join(tail_lines)[:400]
            err_msg = f"Exit {proc.returncode}: {snippet}"
        else:
            err_msg = f"Exit {proc.returncode}"
        return {
            "ok": False,
            "error": err_msg,
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
