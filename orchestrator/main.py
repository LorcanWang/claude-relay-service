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
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from anthropic_client import (
    APP_ACTION_TOOL,
    RUN_COMMAND_TOOL,
    call_anthropic,
    extract_text,
    extract_tool_uses,
)
from executor import execute_command
from session import clear_session, get_session, new_session, save_session
from skill_loader import build_system_prompt
from stream import stream_error, stream_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("orchestrator")

RUNNER_KEY = os.environ.get("RUNNER_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-sonnet-4-6")
MAX_LOOP = int(os.environ.get("MAX_LOOP_ITERATIONS", "10"))

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
        full_system = build_system_prompt(req.systemPrompt, [s.dict() for s in req.enabledSkills])
        tools = [RUN_COMMAND_TOOL, APP_ACTION_TOOL] if req.enabledSkills else [APP_ACTION_TOOL]

        logger.info(
            "Chat [%s] model=%s skills=%s messages=%d",
            session_id, model, enabled_names, len(session["messages"]),
        )

        # ── AI tool loop ──────────────────────────────────────────────────────
        try:
            final_text = None
            collected_actions: list[dict] = []

            for iteration in range(MAX_LOOP):
                logger.info("Loop iteration %d/%d for [%s]", iteration + 1, MAX_LOOP, session_id)

                response = call_anthropic(
                    base_url=req.anthropicConfig.baseURL,
                    auth_token=req.anthropicConfig.authToken,
                    system=full_system,
                    messages=session["messages"],
                    tools=tools,
                    model=model,
                )

                stop_reason = response.get("stop_reason", "end_turn")
                logger.info("Stop reason: %s", stop_reason)

                if stop_reason == "end_turn" or stop_reason not in ("tool_use", "end_turn"):
                    # Final response — extract text and stream it
                    final_text = extract_text(response)
                    session["messages"].append(
                        {"role": "assistant", "content": response.get("content", [])}
                    )
                    break

                if stop_reason == "tool_use":
                    tool_uses = extract_tool_uses(response)
                    if not tool_uses:
                        # Malformed — treat as end
                        final_text = extract_text(response)
                        session["messages"].append(
                            {"role": "assistant", "content": response.get("content", [])}
                        )
                        break

                    # Append assistant message (with tool_use blocks)
                    session["messages"].append(
                        {"role": "assistant", "content": response.get("content", [])}
                    )

                    # Execute all tool calls and collect results
                    tool_results = []
                    for tool in tool_uses:
                        tool_id = tool["id"]
                        tool_name = tool.get("name", "")
                        inp = tool.get("input", {})

                        if tool_name == "app_action":
                            # Collect the action — no subprocess, just acknowledge
                            action = inp.get("action", "")
                            collected_actions.append(inp)
                            logger.info("App action collected: %r", inp)
                            result = {"ok": True, "action": action}
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

                    session["messages"].append({
                        "role": "user",
                        "content": tool_results,
                    })

            else:
                # Hit max iterations without end_turn
                final_text = "I reached the maximum number of steps. Please try a simpler request."
                logger.warning("Max loop iterations reached for [%s]", session_id)

            # ── save session & stream response ────────────────────────────────
            save_session(session_id, session)

            text_to_stream = final_text or ""
            async for chunk in stream_text(text_to_stream, actions=collected_actions or None):
                yield chunk

        except Exception as exc:
            logger.exception("Error in chat loop for [%s]: %s", session_id, exc)
            # Save whatever session state we have
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
