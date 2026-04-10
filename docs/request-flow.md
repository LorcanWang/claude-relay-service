---
title: "Request Flow"
aliases: ["request-flow"]
tags: [architecture, middleware, scheduler, streaming]
created: 2026-04-10
updated: 2026-04-10
status: active
---

# Request Flow

Core request pipeline from client to upstream API and back.

```
Client (cr_ prefix Key) -> Route -> auth middleware -> Unified Scheduler -> Upstream API
```

## Detailed Flow

1. **Client sends request** with `cr_`-prefixed API Key
2. **Route** matches endpoint (Claude, Gemini, OpenAI, etc.)
3. **Auth middleware** (`src/middleware/auth.js`):
   - API Key validation (SHA-256 hash lookup)
   - Permission check
   - Client restriction check
   - Model blacklist check
   - Rate limiting
4. **Unified Scheduler** (`src/services/scheduler/`) selects account:
   - Dedicated account binding (if API Key has one)
   - Group binding (if API Key is bound to a group)
   - [[sticky-sessions|Sticky session]] lookup (if session hash exists)
   - Pool selection (priority-weighted)
5. **Token check/refresh** for OAuth-based accounts
6. **Relay service** (`src/services/relay/`) forwards via proxy
7. **Upstream API** processes and responds
8. **Response handling**:
   - Streaming: SSE transport with real-time usage capture
   - Non-streaming: JSON response
9. **Cost calculation** via `pricingService`
10. **Client disconnect**: AbortController cleanup + concurrency count decrement

## Key Files

| Step | File |
|------|------|
| Routing | `src/routes/api.js`, platform-specific route files |
| Auth | `src/middleware/auth.js` |
| Scheduling | `src/services/scheduler/unifiedClaudeScheduler.js` |
| Relay | `src/services/relay/*RelayService.js` |
| Usage | `src/services/pricingService.js` |
