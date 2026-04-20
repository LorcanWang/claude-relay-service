---
title: "Lynx Agent Protocol"
status: active
updated: 2026-04-20
---

# Lynx Agent Protocol

Standard bidirectional protocol between zeonsolutions frontend, orchestrator, and skill agents.

## Downstream: Context passed TO skills

### Environment Variables (injected by executor.py)

Every skill execution receives these env vars automatically:

| Variable | Description | Example |
|----------|-------------|---------|
| `LYNX_ORG_ID` | Calling organization ID | `fulangkeji` |
| `LYNX_USER_ID` | Calling user's Firebase UID | `abc123def` |
| `LYNX_SESSION_ID` | Current chat session ID | `fulangkeji_abc123def` |
| `LYNX_AGENT_ID` | The skill/agent being executed | `buyer-finder` |
| `LYNX_IN_PLATFORM` | Whether running inside zeonsolutions | `true` or `false` |
| `SKILL_DIR` | Path to skill directory (legacy) | `/home/.../skills/buyer-finder` |

### Per-Skill Config (from org's skillConfigs)

Config values from the frontend are injected as prefixed env vars:

```
skillConfigs: { "amazon-hawk": { "api_key": "abc123" } }
→ LYNX_CONFIG_API_KEY=abc123
→ LYNX_CONFIG_JSON={"api_key":"abc123"}
```

Pattern: `LYNX_CONFIG_{UPPER_SNAKE_KEY}` = value

Skills can read individual vars or parse `LYNX_CONFIG_JSON` for the full config dict.

### Attachments (Phase 10)

When the current user turn carries file uploads (paperclip in chat input), the orchestrator resolves each attachment metadata doc, mints a fresh 7-day signed GET URL, and passes the list as an env var:

```
LYNX_ATTACHMENTS_JSON='[{"id":"uuid","name":"resume.pdf","mimeType":"application/pdf","url":"https://storage.googleapis.com/...?X-Goog-Signature=...","sizeBytes":123456}]'
```

Field contract per entry:

| Field | Description |
|-------|-------------|
| `id` | Attachment doc id — stable for the lifetime of the GCS object |
| `name` | Original filename (safe to display; don't trust as a path) |
| `mimeType` | Server-validated against an allowlist (image/*, pdf, csv, txt, json, docx, xlsx) |
| `url` | Signed GET URL; skill should download to a tmpdir, never re-share upstream |
| `sizeBytes` | Pre-upload reported size (subject to ≤25 MB cap per file) |

Skills that process uploads should:
1. Check `LYNX_ATTACHMENTS_JSON` is set
2. Filter by `mimeType` for relevance
3. Download via the signed URL to a private tmpdir
4. Process, then clean up the tmpdir on exit

If a skill needs to EMIT files back to chat (generated PDFs/images), see the orchestrator output-envelope `attachments` key — documented in `docs/build-plan.md` Phase 10c.

## Upstream: Output FROM skills

### Standard Output Format

Skills should return JSON on stdout:

```json
{
  "ok": true,
  "data": { ... },
  "agentNote": "Found 3 matching buyers",
  "error": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `ok` | boolean | Whether the command succeeded |
| `data` | any | The result payload |
| `agentNote` | string (optional) | One-line summary shown in the agent status rail |
| `error` | string (optional) | Error message when ok=false |

The `agentNote` is emitted as an `agent-status` SSE event with status "completed".

### Backward Compatibility

- Plain text stdout (non-JSON) is auto-wrapped: `{"ok": true, "data": "<text>"}`
- Non-zero exit codes produce: `{"ok": false, "error": "Exit N"}`
- Missing `agentNote` defaults to "Done"

## SSE Events: Orchestrator → Frontend

| Event Type | When | Payload |
|------------|------|---------|
| `agent-roster` | Stream start | `{ agents: [{ id, name, type, seed }] }` |
| `agent-switch` | Before/after tool execution | `{ fromAgentId, toAgentId, reason }` |
| `agent-status` | Status changes | `{ agentId, status, label }` |

Status values: `idle`, `thinking`, `working`, `completed`

## Skill Manifest: agent.json (optional)

Skills can include an `agent.json` in their directory to declare their configuration:

```json
{
  "name": "buyer-finder",
  "description": "Find potential buyers using Hunter.io",
  "agentType": "specialist",
  "executionType": "cli",
  "configFields": [
    {
      "key": "hunter_api_key",
      "label": "Hunter.io API Key",
      "type": "string",
      "required": true,
      "secret": true
    }
  ],
  "permissions": ["read_org", "write_firestore"],
  "outputs": ["contacts", "match_analysis"]
}
```

This manifest is used by the admin UI to show what config fields a skill needs.
Skills without agent.json continue to work — the manifest is additive.
