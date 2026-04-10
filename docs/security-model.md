---
title: "Security Model"
aliases: ["security-model"]
tags: [security, auth, encryption, api-key, jwt]
created: 2026-04-10
updated: 2026-04-10
status: active
---

# Security Model

## Authentication Chain

Every request passes through the full auth chain in `src/middleware/auth.js`:

1. **API Key validation** — `cr_`-prefixed key, SHA-256 hash lookup in Redis
2. **Permission check** — key must have permission for the target service
3. **Client restriction check** — optional client-type validation (e.g. Claude Code only)
4. **Model blacklist** — reject requests for blocked models
5. **Rate limiting** — per-key rate limits (window + max requests)
6. **Concurrency limiting** — per-key concurrent request cap

## Encryption at Rest

- OAuth tokens and refresh tokens: AES encrypted (reference: `claudeAccountService.js`)
- API Keys: SHA-256 hashed, never stored in plaintext
- Encryption key: `ENCRYPTION_KEY` env var, exactly 32 characters

## Token Masking

All log output uses `src/utils/tokenMask.js` to redact sensitive data. Full tokens never appear in logs.

## Client Validation

The `CLAUDE_CODE_MIN_SYSTEM_PROMPT_MATCHES` env var controls Claude Code client validation strictness:
- `0` (default): strict mode — all system prompt entries must pass similarity detection
- `N > 0`: loose mode — at least N entries must match (recommended: 1, for compatibility with billing-header entries in Claude Code 2.1.76+)

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `JWT_SECRET` | JWT signing key (32+ chars) |
| `ENCRYPTION_KEY` | AES encryption key (exactly 32 chars) |
| `REDIS_HOST/PORT/PASSWORD` | Redis connection |
| `API_KEY_PREFIX` | Key prefix (default: `cr_`) |
