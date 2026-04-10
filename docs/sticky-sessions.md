---
title: "Sticky Sessions"
aliases: ["sticky-sessions"]
tags: [scheduler, sticky-session, redis]
created: 2026-04-10
updated: 2026-04-10
status: active
---

# Sticky Sessions

Sticky sessions ensure that requests within the same conversation use the same upstream account, maintaining context continuity.

## How It Works

1. A **session hash** is computed from request content (conversation context)
2. On first request, the [[scheduler-overview|scheduler]] selects an account and stores the mapping in Redis
3. Subsequent requests with the same hash reuse the mapped account
4. If the mapped account becomes unavailable, the mapping is deleted and a new account is selected

## Redis Key Structure

- Key: unified session mapping key with session hash
- Value: `{ accountId, accountType }`
- TTL: auto-extended on each use (smart renewal)

## Cross-Type Considerations

- Non-CCR requests hitting a group with CCR members: CCR sticky mappings are deleted to prevent type mismatch
- `allowCcr` flag controls whether CCR accounts can be selected from Claude groups

## Nginx Requirement

When behind Nginx, add `underscores_in_headers on` to preserve session-related headers.
