---
title: "CCR Scheduling Findings"
aliases: ["ccr-scheduling-findings"]
tags: [ccr, scheduler, review, findings, account-groups]
created: 2026-04-10
updated: 2026-04-10
status: active
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

## Known Gap: Group Path vs Pool Path

The CCR-only pool selection (`_getAvailableCcrAccounts`) has three secondary guards:
- `isAccountRateLimited`
- `isAccountQuotaExceeded`
- `isAccountOverloaded`

The group selection path (`selectAccountFromGroup`) only has:
- `isAccountTemporarilyUnavailable`
- `isAccountRateLimited`

**Missing in group path:** `isAccountOverloaded` and `isAccountQuotaExceeded`. This means a CCR account with `status = 'overloaded'` passes the relaxed status gate AND has no secondary guard — it gets selected and the request fails upstream.

## CCR Status Values

From `ccrAccountService.js`:

| Status | Meaning | Caught by group path? |
|--------|---------|----------------------|
| `active` | Healthy | N/A (passes) |
| `rate_limited` | Hit rate limit | Yes — `isAccountRateLimited` |
| `overloaded` | Upstream 529 | **No** |
| `unauthorized` | Invalid API key | **No** |
| `quota_exceeded` | Daily quota hit | **No** |
| `recovered` | Transitional | N/A (passes, likely OK) |
| `error` | Terminal failure | Yes — status gate |
| `blocked` | Permanently blocked | Yes — status gate |

## Practical Impact

For the user's use case (minimax hitting limits intermittently), the fix is sufficient: the relay no longer 500s the entire group. The worst case is that an overloaded account gets selected and that individual request fails upstream — better than total group failure.

## Future Improvement

Add `isAccountOverloaded` and `isAccountQuotaExceeded` checks to `selectAccountFromGroup` around line 1580, next to the existing `isAccountRateLimited` call, so that bad CCR accounts are *skipped* rather than selected:

```js
if (accountType === 'ccr') {
  const [overloaded, quotaExceeded] = await Promise.all([
    ccrAccountService.isAccountOverloaded(account.id),
    ccrAccountService.isAccountQuotaExceeded(account.id)
  ])
  if (overloaded || quotaExceeded) continue
}
```

## Other PR #4 Observations

- **Scope mismatch**: PR bundles CCR API Key binding (duplicates PR #3), group deletion relaxation, and `CLAUDE_CODE_MIN_SYSTEM_PROMPT_MATCHES` env var — all unrelated to the title
- **`isActive` type inconsistency**: dedicated-CCR binding uses `String(boundCcrAccount.isActive) === 'true'`, group path had `account.isActive === true` for non-official types (fixed to use `String()` for CCR)
- **Dedicated-CCR fallback log**: logs `boundCcrAccount?.isActive` even when account is null (misleading `undefined` in logs)
- **No tests added** for any of the scheduling changes

## Related

- [[scheduler-overview]]
- [[account-types]]
- [[api-key-binding]]
