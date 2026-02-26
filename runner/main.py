#!/usr/bin/env python3
"""
Skill Runner — FastAPI service that executes skills as subprocesses.

Environment variables:
  SKILL_ROOT   Path to skill folders (default: /home/hqzn/grantllama-scrape-skill/.claude/skills)
  RUNNER_KEY   Bearer token for auth (required in production)
  PORT         Port to listen on (default: 8080)

Entrypoint priority per skill folder: run.sh > run.py > index.js > main.py
Args are passed via SKILL_ARGS_JSON env var to the subprocess.
ORG_ID and USER_ID are also injected if provided.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

SKILL_ROOT = os.environ.get(
    "SKILL_ROOT",
    "/home/hqzn/grantllama-scrape-skill/.claude/skills",
)
RUNNER_KEY = os.environ.get("RUNNER_KEY", "")
TIMEOUT = int(os.environ.get("SKILL_TIMEOUT", "60"))

app = FastAPI(title="Skill Runner", version="1.0.0")
security = HTTPBearer(auto_error=False)


def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not RUNNER_KEY:
        return None  # No auth configured — open
    if credentials is None or credentials.credentials != RUNNER_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")
    return credentials.credentials


class RunSkillRequest(BaseModel):
    name: str
    args: dict = {}
    orgId: Optional[str] = None
    userId: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/skills")
def list_skills(_: Any = Depends(verify_token)):
    """List available skills (folders with a recognised entrypoint)."""
    root = Path(SKILL_ROOT)
    if not root.exists():
        return {"skills": [], "skill_root": str(root), "error": "SKILL_ROOT not found"}

    skills = []
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir():
            continue
        entrypoint = _find_entrypoint(skill_dir)
        skills.append(
            {
                "name": skill_dir.name,
                "entrypoint": entrypoint.name if entrypoint else None,
                "ready": entrypoint is not None,
            }
        )
    return {"skills": skills, "skill_root": str(root)}


@app.post("/run_skill")
def run_skill(req: RunSkillRequest, _: Any = Depends(verify_token)):
    skill_dir = Path(SKILL_ROOT) / req.name
    if not skill_dir.exists():
        return {"ok": False, "error": f"Skill '{req.name}' not found at {skill_dir}"}

    entrypoint = _find_entrypoint(skill_dir)
    if entrypoint is None:
        return {
            "ok": False,
            "error": f"No entrypoint found in '{req.name}' (need run.sh / run.py / index.js / main.py)",
        }

    env = os.environ.copy()
    env["SKILL_ARGS_JSON"] = json.dumps(req.args)
    if req.orgId:
        env["ORG_ID"] = req.orgId
    if req.userId:
        env["USER_ID"] = req.userId

    cmd = _build_cmd(entrypoint)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
            cwd=str(skill_dir),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Skill '{req.name}' timed out after {TIMEOUT}s"}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to execute skill: {exc}"}

    if result.returncode != 0:
        return {
            "ok": False,
            "error": result.stderr.strip() or f"Skill exited with code {result.returncode}",
            "stdout": result.stdout.strip() or None,
        }

    stdout = result.stdout.strip()
    if not stdout:
        return {"ok": True, "data": None}

    try:
        data = json.loads(stdout)
        return {"ok": True, "data": data}
    except json.JSONDecodeError:
        # Not JSON — return raw text
        return {"ok": True, "data": {"output": stdout}}


# ── helpers ──────────────────────────────────────────────────────────────────

ENTRYPOINTS = ["run.sh", "run.py", "index.js", "main.py"]


def _find_entrypoint(skill_dir: Path) -> Optional[Path]:
    for name in ENTRYPOINTS:
        p = skill_dir / name
        if p.exists():
            return p
    return None


def _build_cmd(entrypoint: Path) -> list:
    suffix = entrypoint.suffix
    if suffix == ".py":
        return [sys.executable, str(entrypoint)]
    if suffix == ".sh":
        return ["bash", str(entrypoint)]
    if suffix == ".js":
        return ["node", str(entrypoint)]
    # Fallback: try direct execution
    return [str(entrypoint)]


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
