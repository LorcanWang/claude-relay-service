# VPS Architecture — Lynx / GrantLlama AI Stack

## Overview

Three services run on this VPS (`192.168.193.188`), forming the backend for the Zeon/Lynx chat frontend.

```
Browser (Next.js frontend)
    │
    │  POST /chat  (SSE stream)
    ▼
┌─────────────────────────────────────┐
│  Orchestrator  :8090                │
│  orchestrator/main.py               │
│  • Loads session from Redis         │
│  • Reads SKILL.md from disk         │
│  • Runs Anthropic tool loop         │
│  • Executes skill commands locally  │
│  • Streams final response           │
└──────────────┬──────────────────────┘
               │  POST /v1/messages
               ▼
┌─────────────────────────────────────┐
│  Claude Relay Service  :3000        │
│  src/app.js                         │
│  • API key auth                     │
│  • Multi-account routing            │
│  • Session stickiness               │
│  • Forwards to Anthropic API        │
└──────────────┬──────────────────────┘
               │
               ▼
         Anthropic API
         (api.anthropic.com)

Also running:
┌─────────────────────────────────────┐
│  Skill Runner  :8081                │
│  runner/main.py                     │
│  • Simple HTTP wrapper around       │
│    skill run.py files               │
│  • Used for direct skill calls      │
│    (not used by orchestrator)       │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│  Redis  :6379 (Docker)              │
│  • Relay: accounts, API keys,       │
│    concurrency, session binding     │
│  • Orchestrator: chat sessions      │
└─────────────────────────────────────┘
```

---

## Services

### 1. Claude Relay Service (port 3000)

**What it does:** Acts as a proxy between clients and the Anthropic API. Manages a pool of Claude accounts, handles auth via API keys (`cr_` prefix), enforces rate limits, and maintains sticky sessions.

**Key modifications made to support Lynx/Zeon:**
- `src/services/relay/claudeRelayService.js` — preserves client system prompts instead of always injecting the Claude Code prompt; skips tool name transformation for API clients (so `run_command` isn't mangled)
- `src/routes/api.js` — accepts `x-session-id` header for stable sticky sessions; skips `isOldSession` check for API clients with multi-turn history
- `src/middleware/auth.js` — allows API clients with a custom system prompt through the Claude-Code-only gate
- `src/utils/sessionHelper.js` — hashes `api_` prefixed `metadata.user_id` for stable session routing

**Start/restart:**
```bash
cd /home/hqzn/claude-relay-service
node scripts/manage.js restart -d
node scripts/manage.js status
node scripts/manage.js logs
```

**Admin UI:** http://192.168.193.188:3000/web

---

### 2. Orchestrator (port 8090)

**What it does:** The AI brain. Receives a chat request from Next.js, runs the full Anthropic tool loop, executes skill commands, maintains multi-turn session history, and streams the response back.

**Files:**
```
orchestrator/
  main.py              FastAPI app — /health, /chat, /sessions/{id}
  session.py           Redis session store (in-memory fallback), 24h TTL
  anthropic_client.py  httpx call to relay, defines run_command tool schema
  skill_loader.py      Reads SKILL.md from disk, builds full system prompt
  executor.py          Runs shell commands in skill directories
  stream.py            Formats AI SDK SSE events for the browser
  requirements.txt
```

**The loop (`main.py`):**
1. Load session by `{orgId}_{userId}` from Redis
2. Append new user messages
3. Build full system prompt = `systemPrompt` + each skill's `SKILL.md` + tool instructions
4. Call Anthropic via relay (non-streaming)
5. If `stop_reason == tool_use` → execute the command in the skill dir → append result → loop
6. If `stop_reason == end_turn` → chunk text into SSE events → stream to Next.js
7. Save updated session

**Tool:** one tool registered with Anthropic:
```json
{
  "name": "run_command",
  "input": {
    "skill": "google-ad-campaign",
    "command": "python3 ads.py list_campaigns --status ENABLED"
  }
}
```
The model reads `SKILL.md` to know which commands to run. The orchestrator just executes them.

**Start/restart:**
```bash
cd /home/hqzn/claude-relay-service/orchestrator
RUNNER_KEY=34d613fd2081d70ab21e6dedc8ec4b41286e2898db62ed6d \
SKILL_ROOT=/home/hqzn/grantllama-scrape-skill/.claude/skills \
ORCHESTRATOR_PORT=8090 \
nohup python3 main.py >> orchestrator.log 2>&1 &
echo $! > orchestrator.pid
```

Or use the manage-skills skill:
```bash
python3 .claude/skills/manage-skills/manage.py reload-orchestrator
```

**API from Next.js:**
```
POST http://192.168.193.188:8090/chat
Authorization: Bearer 34d613fd2081d70ab21e6dedc8ec4b41286e2898db62ed6d

{
  "messages": [{"role":"user","parts":[{"type":"text","text":"List my campaigns"}]}],
  "systemPrompt": "You are Lynx...",
  "enabledSkills": [{"name":"google-ad-campaign"},{"name":"web-search"}],
  "anthropicConfig": {
    "baseURL": "http://192.168.193.188:3000/api/v1",
    "authToken": "cr_..."
  },
  "orgId": "org_123",
  "userId": "user_abc"
}
```

**Clear a session:**
```
DELETE http://192.168.193.188:8090/sessions/org_123_user_abc
```

Or pass `"clearSession": true` in the chat request body.

---

### 3. Skill Runner (port 8081)

**What it does:** Simple HTTP API that executes skills directly via their `run.py` entrypoints, without going through Claude. Useful for direct skill calls from frontend or testing.

**Files:**
```
runner/
  main.py          FastAPI app — /health, /skills, /run_skill
  requirements.txt
  runner.pid
  runner.log
```

**API:**
```
GET  /health                           → {"status":"ok"}
GET  /skills                           → list of skills with ready status
POST /run_skill                        → execute a skill
  Body: {"name":"web-search","args":{"action":"search","query":"..."}}
```

**Start/restart:**
```bash
cd /home/hqzn/claude-relay-service/runner
RUNNER_KEY=34d613fd2081d70ab21e6dedc8ec4b41286e2898db62ed6d \
SKILL_ROOT=/home/hqzn/grantllama-scrape-skill/.claude/skills \
PORT=8081 \
nohup python3 main.py >> runner.log 2>&1 &
echo $! > runner.pid
```

---

## Skills

All skills live at `/home/hqzn/grantllama-scrape-skill/.claude/skills/`.

Each skill folder contains:
- `SKILL.md` or `skill.md` — documentation the orchestrator injects into the system prompt
- `run.py` — thin entrypoint for the runner API (reads `SKILL_ARGS_JSON` env var)
- One or more `*.py` files — the actual skill implementation

### Current skills (14/16 ready)

| Skill | run.py | Main file | Description |
|---|---|---|---|
| `web-search` | ✅ | `web_search.py` | DuckDuckGo search/news/fetch |
| `scrape-grants` | ✅ | `bq_helpers.py` | Foundation grant scraping |
| `grantllama` | ✅ | `bq_helpers.py` | BigQuery grant data queries |
| `tiktok-ads` | ✅ | `tiktok_ads.py` | TikTok campaign management |
| `buyer-finder` | ✅ | `hunter_api.py`, `firestore_helpers.py` | Hunter.io buyer discovery |
| `gmail` | ✅ | `gmail_api.py` | Multi-user Gmail integration |
| `google-ad-campaign` | ✅ | `ads.py` | Google Ads campaign management |
| `google-seo-keywords` | ✅ | `seo_helpers.py`, `dataforseo.py` | SEO keyword research |
| `recruiting` | ✅ | `search.py` | BOSS直聘 candidate search |
| `amazon-insights` | ✅ | `bq_helpers.py`, `reddit_search.py` | Amazon opportunity discovery |
| `amazon-keywords` | ✅ | `js_helpers.py` | Jungle Scout keyword expansion |
| `issue-tracker` | ✅ | `issues.py` | Firestore issue tracker |
| `deploy-clawdbot` | ✅ | `deploy.py` | Docker clawdbot deployment |
| `stitch-video` | ✅ | `stitch.py` | FFmpeg video stitching |
| `gsc-indexing` | ❌ | TypeScript only | Google Search Console |
| `notebooklm` | ❌ | No Python API | NotebookLM integration |

---

## Auth

All three services share the same bearer token stored in `.env`:

```
RUNNER_KEY=34d613fd2081d70ab21e6dedc8ec4b41286e2898db62ed6d
```

Pass this in every request:
```
Authorization: Bearer 34d613fd2081d70ab21e6dedc8ec4b41286e2898db62ed6d
```

The relay service uses separate auth: API keys with `cr_` prefix managed via the admin UI at `:3000/web`.

---

## Environment (`.env`)

```
PORT=3000
HOST=0.0.0.0
NODE_ENV=production

JWT_SECRET=<generated>
ENCRYPTION_KEY=<generated>

REDIS_HOST=127.0.0.1
REDIS_PORT=6379

RUNNER_KEY=34d613fd2081d70ab21e6dedc8ec4b41286e2898db62ed6d
RUNNER_PORT=8081
ORCHESTRATOR_PORT=8090
SKILL_ROOT=/home/hqzn/grantllama-scrape-skill/.claude/skills
```

---

## Managing Skills

Use the `manage-skills` Claude Code skill to inspect and update skills:

```bash
# List all skills and their runner status
python3 .claude/skills/manage-skills/manage.py list

# Inspect Python functions in a skill (to write a new run.py)
python3 .claude/skills/manage-skills/manage.py inspect gsc-indexing

# Show existing run.py
python3 .claude/skills/manage-skills/manage.py show web-search

# Restart runner after adding a new skill
python3 .claude/skills/manage-skills/manage.py reload-runner
```

### Adding a new skill from grantllama-scrape-skill

1. The skill folder already exists in `SKILL_ROOT` — check it has a `SKILL.md`
2. Inspect its Python files: `manage.py inspect <skill-name>`
3. Write a `run.py` in the skill folder (see existing ones as templates)
4. Reload the runner: `manage.py reload-runner`
5. Verify: `curl http://localhost:8081/skills`

---

## Session Flow (end-to-end)

```
User types in browser
  → Next.js resolves user/org from Firestore, builds systemPrompt
  → POST /chat to Orchestrator :8090

Orchestrator:
  → loads session org_123_user_abc from Redis (or creates new)
  → appends user message to session.messages
  → reads SKILL.md for each enabledSkill from disk
  → builds full_system = systemPrompt + skill docs + tool instructions
  → POST /v1/messages to Relay :3000 (non-streaming)
      → Relay picks an available Claude account
      → forwards to Anthropic API

  Anthropic responds with stop_reason="tool_use":
  → run_command(skill="google-ad-campaign", command="python3 ads.py list_campaigns")
  → Orchestrator executes command in /skills/google-ad-campaign/ (subprocess, 60s timeout)
  → appends tool result to session.messages
  → loops back to Anthropic

  Anthropic responds with stop_reason="end_turn":
  → Orchestrator chunks text into SSE events
  → saves updated session to Redis (24h TTL)
  → streams SSE back to Next.js → browser

Browser's useChat hook renders the streamed response.
```
