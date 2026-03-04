#!/usr/bin/env python3
"""
Orchestrator — handles the full AI loop for the Zeon/Lynx chat frontend.

POST /chat  — receive messages from Next.js, run Anthropic tool loop, stream response
GET  /health — liveness check
GET  /sessions/{session_id} — inspect a session (dev helper)
DELETE /sessions/{session_id} — clear a session

Environment:
  RUNNER_KEY        Bearer token for auth from Next.js
  SKILL_ROOT        Path to skill folders
  SESSION_TTL_SECONDS  Session TTL (default 86400)
  DEFAULT_MODEL     Anthropic model (default claude-sonnet-4-6)
  MAX_LOOP_ITERATIONS  Max tool loop cycles (default 10)
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Load .env from orchestrator directory if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from anthropic_client import (
    APP_ACTION_TOOL,
    AnthropicStream,
    RUN_COMMAND_TOOL,
    call_anthropic,
    extract_text,
)
from executor import execute_command
from session import clear_session, get_session, new_session, save_session
from skill_loader import build_system_prompt
from stream import sse, stream_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("orchestrator")

RUNNER_KEY = os.environ.get("RUNNER_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-sonnet-4-6")
MAX_LOOP = int(os.environ.get("MAX_LOOP_ITERATIONS", "50"))
# Compaction: when stored messages exceed this, summarize old ones like Claude Code CLI does.
COMPACT_THRESHOLD = int(os.environ.get("COMPACT_THRESHOLD", "40"))
COMPACT_KEEP_RECENT = int(os.environ.get("COMPACT_KEEP_RECENT", "10"))
# Override relay URL to bypass Cloudflare/CDN gzip compression on SSE streams.
# If set, this replaces the baseURL sent by the frontend.
RELAY_BASE_URL = os.environ.get("RELAY_BASE_URL", "")

app = FastAPI(title="Orchestrator", version="1.0.0")
security = HTTPBearer(auto_error=False)


# ── auth ──────────────────────────────────────────────────────────────────────

def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not RUNNER_KEY:
        return None
    if credentials is None or credentials.credentials != RUNNER_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")
    return credentials.credentials


# ── request models ────────────────────────────────────────────────────────────

class UIPart(BaseModel):
    type: str
    text: Optional[str] = None


class UIMessage(BaseModel):
    id: Optional[str] = None
    role: str
    parts: Optional[list[UIPart]] = None
    content: Optional[str] = None  # fallback for plain string content


class AnthropicConfig(BaseModel):
    baseURL: str
    authToken: str
    model: Optional[str] = None


class SkillMeta(BaseModel):
    name: str
    description: Optional[str] = ""


class ChatRequest(BaseModel):
    messages: list[UIMessage]
    systemPrompt: str
    enabledSkills: list[SkillMeta] = []
    anthropicConfig: AnthropicConfig
    sessionId: Optional[str] = None   # preferred: pre-built by frontend
    orgId: Optional[str] = None       # fallback: construct session key from these
    userId: Optional[str] = None
    inPlatform: Optional[bool] = False
    clearSession: Optional[bool] = False


# ── message conversion ────────────────────────────────────────────────────────

def ui_to_anthropic(msg: UIMessage) -> dict:
    """Convert UI message format to Anthropic API format."""
    role = msg.role
    if msg.content:
        # Already a plain string
        return {"role": role, "content": msg.content}
    if msg.parts:
        text = " ".join(p.text for p in msg.parts if p.type == "text" and p.text)
        return {"role": role, "content": text}
    return {"role": role, "content": ""}


# ── session compaction ────────────────────────────────────────────────────────

def compact_session(session: dict, base_url: str, auth_token: str, model: str) -> bool:
    """
    When session exceeds COMPACT_THRESHOLD messages, summarize the older ones
    and replace them with a compact context block — preserving the last
    COMPACT_KEEP_RECENT messages verbatim. Returns True if compacted.
    Mirrors how Claude Code CLI handles long context windows.
    """
    messages = session.get("messages", [])
    if len(messages) <= COMPACT_THRESHOLD:
        return False

    to_summarize = messages[:-COMPACT_KEEP_RECENT]
    keep_recent = messages[-COMPACT_KEEP_RECENT:]

    logger.info(
        "Compacting session [%s]: summarizing %d messages, keeping %d recent",
        session.get("session_id", "?"), len(to_summarize), len(keep_recent),
    )

    try:
        resp = call_anthropic(
            base_url=base_url,
            auth_token=auth_token,
            system=(
                "You are a conversation summarizer. "
                "Produce a concise but complete summary of the conversation below. "
                "Preserve: key facts, decisions made, data retrieved (IDs, names, numbers), "
                "tasks completed, and any unresolved items. "
                "Write in third person past tense. Be dense — this replaces the raw history."
            ),
            messages=[
                {"role": "user", "content": (
                    "Summarize this conversation:\n\n" +
                    "\n".join(
                        f"[{m['role'].upper()}]: " + (
                            m["content"] if isinstance(m["content"], str)
                            else str([b.get("text", b.get("type", "")) for b in m["content"]])
                        )
                        for m in to_summarize
                    )
                )}
            ],
            model=model,
        )
        summary_text = extract_text(resp)
    except Exception as exc:
        logger.warning("Compaction summarization failed, skipping: %s", exc)
        return False

    compact_block = [
        {
            "role": "user",
            "content": (
                "<conversation_summary>\n"
                "The following is a summary of the conversation so far:\n\n"
                f"{summary_text}\n"
                "</conversation_summary>"
            ),
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context from our previous conversation and will continue from here.",
        },
    ]

    session["messages"] = compact_block + keep_recent
    session["compact_count"] = session.get("compact_count", 0) + 1
    logger.info("Compaction done. Session now has %d messages.", len(session["messages"]))
    return True


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/sessions/{session_id}")
def get_session_info(session_id: str, _=Depends(verify_token)):
    data = get_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": data["session_id"],
        "message_count": len(data.get("messages", [])),
        "created_at": data.get("created_at"),
        "last_active": data.get("last_active"),
    }


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, _=Depends(verify_token)):
    clear_session(session_id)
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest, _=Depends(verify_token)):
    async def event_stream():
        session_id = req.sessionId or f"{req.orgId}_{req.userId}"
        enabled_names = [s.name for s in req.enabledSkills]
        model = req.anthropicConfig.model or DEFAULT_MODEL

        # ── session ───────────────────────────────────────────────────────────
        if req.clearSession:
            clear_session(session_id)

        session = get_session(session_id) or new_session(session_id)

        # Only append the LAST user message from this request.
        # The session already contains full history from Redis;
        # the frontend (useChat) sends all messages each time,
        # so appending them all would cause duplicates.
        user_messages = [msg for msg in req.messages if msg.role == "user"]
        if user_messages:
            session["messages"].append(ui_to_anthropic(user_messages[-1]))

        # ── build full system prompt ──────────────────────────────────────────
        full_system = build_system_prompt(
            req.systemPrompt,
            [s.dict() for s in req.enabledSkills],
            org_id=req.orgId,
            user_id=req.userId,
            in_platform=req.inPlatform,
        )
        tools = [RUN_COMMAND_TOOL, APP_ACTION_TOOL] if req.enabledSkills else [APP_ACTION_TOOL]

        # ── compact if session is getting long ────────────────────────────────
        compacted = compact_session(
            session,
            base_url=RELAY_BASE_URL or req.anthropicConfig.baseURL,
            auth_token=req.anthropicConfig.authToken,
            model=model,
        )
        if compacted:
            save_session(session_id, session)

        logger.info(
            "Chat [%s] model=%s skills=%s messages=%d%s",
            session_id, model, enabled_names, len(session["messages"]),
            " (compacted)" if compacted else "",
        )

        # ── AI tool loop (all iterations stream live) ─────────────────────────
        try:
            collected_actions: list[dict] = []
            step_id = 0

            api_kwargs = dict(
                base_url=RELAY_BASE_URL or req.anthropicConfig.baseURL,
                auth_token=req.anthropicConfig.authToken,
                system=full_system,
                tools=tools,
                model=model,
            )

            yield sse({"type": "start"})

            for iteration in range(MAX_LOOP):
                logger.info("Loop iteration %d/%d for [%s]", iteration + 1, MAX_LOOP, session_id)

                # Every iteration streams from Anthropic in real-time
                stream = AnthropicStream(messages=session["messages"], **api_kwargs)

                yield sse({"type": "start-step"})
                text_id = str(step_id)
                has_text = False

                async for delta in stream:
                    if not has_text:
                        yield sse({"type": "text-start", "id": text_id})
                        has_text = True
                    yield sse({"type": "text-delta", "id": text_id, "delta": delta})

                if has_text:
                    yield sse({"type": "text-end", "id": text_id})
                step_id += 1

                stop_reason = stream.stop_reason
                logger.info("Stop reason: %s (iteration %d)", stop_reason, iteration + 1)

                # Persist assistant message to session (skip if empty — Anthropic rejects blank content)
                if stream.content:
                    session["messages"].append(
                        {"role": "assistant", "content": stream.content}
                    )

                if stop_reason != "tool_use":
                    # ── end_turn: emit actions + finish ──────────────────────
                    if collected_actions:
                        for action in collected_actions:
                            yield sse({"type": "data-action", "data": action})
                    yield sse({"type": "finish-step"})
                    yield sse({"type": "finish", "finishReason": "stop"})
                    yield sse("[DONE]")
                    break

                # ── tool_use: extract and execute ────────────────────────────
                tool_uses = stream.tool_uses
                if not tool_uses:
                    # Edge case: stop_reason=tool_use but no blocks found
                    if collected_actions:
                        for action in collected_actions:
                            yield sse({"type": "data-action", "data": action})
                    yield sse({"type": "finish-step"})
                    yield sse({"type": "finish", "finishReason": "stop"})
                    yield sse("[DONE]")
                    break

                yield sse({"type": "finish-step"})

                # ── Execute tool calls ───────────────────────────────────────
                tool_results = []
                for tool in tool_uses:
                    tool_id = tool["id"]
                    tool_name = tool.get("name", "")
                    inp = tool.get("input", {})

                    if tool_name == "app_action":
                        collected_actions.append(inp)
                        logger.info("App action collected: %r", inp)
                        result = {"ok": True, "action": inp.get("action", "")}
                    else:
                        skill_name = inp.get("skill", "")
                        command = inp.get("command", "")
                        logger.info("Tool call: skill=%r command=%r", skill_name, command)
                        result = execute_command(skill_name, command, enabled_names)
                        logger.info("Tool result ok=%s", result.get("ok"))

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })

                session["messages"].append({"role": "user", "content": tool_results})

            else:
                # Hit max iterations without end_turn
                yield sse({"type": "start-step"})
                text_id = str(step_id)
                yield sse({"type": "text-start", "id": text_id})
                yield sse({"type": "text-delta", "id": text_id, "delta": "I reached the maximum number of steps. Please try a simpler request."})
                yield sse({"type": "text-end", "id": text_id})
                yield sse({"type": "finish-step"})
                yield sse({"type": "finish", "finishReason": "stop"})
                yield sse("[DONE]")
                logger.warning("Max loop iterations reached for [%s]", session_id)

            save_session(session_id, session)

        except Exception as exc:
            logger.exception("Error in chat loop for [%s]: %s", session_id, exc)
            try:
                save_session(session_id, session)
            except Exception:
                pass
            async for chunk in stream_error(str(exc)):
                yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("ORCHESTRATOR_PORT", "8090"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
