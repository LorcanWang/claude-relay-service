---
title: "CCR Scheduling Findings"
aliases: ["ccr-scheduling-findings"]
tags: [ccr, scheduler, review, findings, account-groups]
created: 2026-04-10
updated: 2026-04-13
status: resolved
---

# CCR Scheduling Findings

Review findings from PR #4 (`fix: remove status check for CCR accounts in group selection`), reviewed 2026-04-09/10.

## Problem

When a CCR account (e.g. minimax) hit its rate/quota limit and its `status` was set to `overloaded`, the `selectAccountFromGroup` method's strict `status === 'active'` gate filtered it out. If it was the only CCR member (or all members were affected), the entire group returned "No available accounts" and the relay responded with 500.

## Fix Applied

Changed the CCR status gate in `selectAccountFromGroup` from:
```js
account.status === 'active'
```
to:
```js
account.status !== 'error' && account.status !== 'blocked'
```

This matches the `claude-official` branch semantics — terminal failure statuses are excluded, but recoverable statuses (`overloaded`, `rate_limited`, etc.) pass through to the secondary checks.

## Resolved: Group Path vs Pool Path (fixed in `cd8681b`)

The CCR-only pool selection (`_getAvailableCcrAccounts`) has three secondary guards:
- `isAccountRateLimited`
- `isAccountQuotaExceeded`
- `isAccountOverloaded`

The group selection path (`selectAccountFromGroup`) originally only had:
- `isAccountTemporarilyUnavailable`
- `isAccountRateLimited`

**Fixed (2026-04-10):** Author added `isAccountQuotaExceeded` and `isAccountOverloaded` checks to the group path per review feedback. Both CCR and MiniMax account types now have parity between the pool and group selection paths.

## CCR Status Values

From `ccrAccountService.js`:

| Status | Meaning | Caught by group path? |
|--------|---------|----------------------|
| `active` | Healthy | N/A (passes) |
| `rate_limited` | Hit rate limit | Yes — `isAccountRateLimited` |
| `overloaded` | Upstream 529 | Yes — `isAccountOverloaded` (added `cd8681b`) |
| `unauthorized` | Invalid API key | Passes status gate (not `error`/`blocked`) |
| `quota_exceeded` | Daily quota hit | Yes — `isAccountQuotaExceeded` (added `cd8681b`) |
| `recovered` | Transitional | N/A (passes, likely OK) |
| `error` | Terminal failure | Yes — status gate |
| `blocked` | Permanently blocked | Yes — status gate |

## Practical Impact

For the user's use case (minimax hitting limits intermittently), the fix is sufficient: the relay no longer 500s the entire group. The worst case is that an overloaded account gets selected and that individual request fails upstream — better than total group failure.

## Resolution

All recommended fixes were implemented by the PR author in commit `cd8681b`:
- `isAccountQuotaExceeded` and `isAccountOverloaded` added to group path for CCR
- MiniMax accounts also received the same guards
- PR #4 merged 2026-04-13, PRs #1 and #3 closed as superseded

## Other PR #4 Observations (accepted at merge)

- **Scope**: PR bundled CCR API Key binding, group deletion relaxation, and `CLAUDE_CODE_MIN_SYSTEM_PROMPT_MATCHES` env var — accepted as a consolidated PR after #1 and #3 were closed
- **`isActive` type inconsistency**: fixed to use `String()` for CCR in group path
- **Dedicated-CCR fallback log**: logs `boundCcrAccount?.isActive` when account is null (minor, cosmetic)
- **No tests added** — acknowledged, not blocking for this fix

## Related

- [[scheduler-overview]]
- [[account-types]]
- [[api-key-binding]]
