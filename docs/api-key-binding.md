---
title: "API Key Account Binding"
aliases: ["api-key-binding"]
tags: [api-key, scheduler, architecture]
created: 2026-04-10
updated: 2026-04-10
status: active
---

# API Key Account Binding

API Keys (`cr_` prefix) can be bound to specific accounts or groups per platform, overriding pool selection.

## Binding Fields

Each API Key record in Redis can have these binding fields:

| Field | Platform | Values |
|-------|----------|--------|
| `claudeAccountId` | Claude | Account ID or `group:<groupId>` |
| `claudeConsoleAccountId` | Claude Console | Account ID or `group:<groupId>` |
| `geminiAccountId` | Gemini | Account ID or `group:<groupId>` |
| `openaiAccountId` | OpenAI | Account ID or `group:<groupId>` |
| `bedrockAccountId` | Bedrock | Account ID |
| `droidAccountId` | Droid | Account ID or `group:<groupId>` |
| `ccrAccountId` | CCR | Account ID or `group:<groupId>` |

## Binding Modes

1. **No binding** (empty) — use shared pool
2. **Direct binding** (`accountId`) — always use this specific account; fall back to pool if unavailable
3. **Group binding** (`group:<groupId>`) — select from group members via [[scheduler-overview|scheduler]] priority logic

## Scheduler Resolution

In `_selectClaudeAccountByPriority`, bindings are checked in order:
1. Claude official dedicated/group
2. Claude Console dedicated/group
3. Droid dedicated/group
4. CCR dedicated/group (added in PR #4)
5. Sticky session fallback
6. Pool selection

## Related

- [[scheduler-overview]]
- [[account-types]]
- [[ccr-scheduling-findings]]
