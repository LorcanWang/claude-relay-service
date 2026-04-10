---
title: "Account Types"
aliases: ["account-types"]
tags: [architecture, claude, gemini, openai, bedrock, azure, droid, ccr]
created: 2026-04-10
updated: 2026-04-10
status: active
---

# Account Types

The relay service supports multiple upstream AI API providers, each with its own account service and relay service.

## Supported Platforms

| Platform | Account Service | Relay Service | Auth Method |
|----------|----------------|---------------|-------------|
| Claude (Official) | `claudeAccountService.js` | `claudeRelayService.js` | OAuth (encrypted tokens) |
| Claude Console | `claudeConsoleAccountService.js` | `claudeConsoleRelayService.js` | Session key |
| CCR | `ccrAccountService.js` | `ccrRelayService.js` | API Key |
| Gemini | `geminiAccountService.js` | `geminiRelayService.js` | API Key |
| OpenAI | `openaiAccountService.js` | `openaiRelayService.js` | API Key |
| OpenAI Responses | `openaiResponsesAccountService.js` | `openaiResponsesRelayService.js` | API Key |
| AWS Bedrock | `bedrockAccountService.js` | `bedrockRelayService.js` | IAM credentials |
| Azure OpenAI | `azureOpenaiAccountService.js` | `azureOpenaiRelayService.js` | API Key |
| Droid | `droidAccountService.js` | `droidRelayService.js` | API Key |

## Account Modes

Each account can be:
- **Shared** — available to all API Keys via pool selection
- **Dedicated** — bound to a specific API Key
- **Group** — member of an account group for grouped scheduling

## Platform Grouping

Claude, Claude Console, and CCR accounts can coexist in the same "claude" platform group. See [[scheduler-overview]] for how the [[scheduler-overview|unified scheduler]] selects among mixed types.

## isActive Type Inconsistency

Different platforms store `isActive` differently:
- `claude-official` and `ccr`: stored as string `'true'`/`'false'`
- Other types: stored as boolean `true`/`false`

The [[scheduler-overview|scheduler]] handles this via type-specific checks. See [[ccr-scheduling-findings]] for related issues.
