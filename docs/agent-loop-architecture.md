---
title: "Agent Loop Architecture"
aliases: ["agent-loop", "tool-loop", "agentic-loop"]
tags: [architecture, orchestrator, tool-use, claude, agent-loop]
updated: 2026-04-27
status: active
---

# Agent Loop Architecture

> Research-backed design for Lynx's agent tool loop, synthesized from
> Claude Code, Anthropic Agent SDK, Hermes, OpenAI Agents SDK, Vercel AI SDK,
> and LangGraph. Based on analysis of 11 independent research agents.

## Industry Consensus

Every production agent runner follows the same core pattern:

| Framework | Continue signal | Stop signal | Uses stop_reason? |
|-----------|----------------|-------------|-------------------|
| **Claude Code** | `tool_use` blocks in content | No tool_use blocks | No (explicit comment: unreliable) |
| **Anthropic Agent SDK** | `stop_reason == "tool_use"` | Branches on all stop_reasons | Yes, but full state machine |
| **Hermes (NousResearch)** | `tool_calls` present | No tool_calls | No, content inspection |
| **OpenAI Agents SDK** | Typed next-step discriminator | `NextStepFinalOutput` | No, item-driven |
| **Vercel AI SDK** | Client tool calls resolved | Zero tool calls | No |
| **LangGraph** | `tool_calls` in last message | No tool_calls | No |
| **OpenClaw** | `stopReason === "toolUse"` | `"stop"` + no incomplete turn | Yes, but with 3 nudge types |

**Universal rule**: the loop continues when `tool_use` blocks exist in the response
content. Text-only responses are terminal by default. Only OpenClaw/Hermes add
planning-only detection nudges on top of this base pattern.

## Claude Code's Loop (Reference Implementation)

Source: `liuup/claude-code-analysis`, VILA-Lab paper (arXiv:2604.14228),
PromptLayer deep-dive, source-leak analyses.

### Structure

```
while (true) {
    apiMessages = normalizeMessagesForAPI(messages)
    response = await claudeApi.stream(apiMessages, systemPrompt)

    toolUseBlocks = extractToolUseBlocks(response.content)
    if (toolUseBlocks.length === 0) break   // ← THE exit condition

    toolResults = await runTools(toolUseBlocks)
    messages = [...messages, ...toolResults]

    if (shouldCompact(messages)) await compact(messages)
}
```

Key properties:
- **`while (true)`**, not `for i in range(N)`
- Does NOT trust `stop_reason` — inspects content blocks directly
- `needsFollowUp` flag set when tool_use blocks found
- No generic "work seems incomplete" heuristic
- Tool errors become `tool_result` messages fed back to model (never crash the loop)

### Stop Reasons Handling

| stop_reason | Action |
|-------------|--------|
| `end_turn` (no tool_use) | Exit loop — model is done |
| `tool_use` | Execute tools, continue |
| `max_tokens` | Retry at higher capacity (8K → 64K), up to 3 retries |
| `pause_turn` | Continue (resend assistant content as-is) |
| `refusal` | Exit with refusal |

### Safety Limits

- `max_turns` — caps tool-use turns only (text responses don't count)
- `max_budget_usd` — caps spend
- Token budget tracking via `tokenBudget.ts` (not iteration counting)
- Five-layer compaction pipeline at 92% context usage

### Tool Execution

- Read-only tools run **concurrently** via `partitionToolCalls()`
- State-modifying tools run **sequentially**
- Tool errors normalized into `tool_result` — model decides what to do
- Per-tool isolation — one failure doesn't kill the batch

## Hermes Agent (NousResearch)

Source: `NousResearch/hermes-agent`, `eikarna/hermes-rs`

### Key Patterns

Same core loop: `while tool_calls: execute → continue; else: break`

Notable additions:
- **`_looks_like_codex_intermediate_ack()`** — detects planning text without
  tool calls, injects nudge: `"[System: Continue now. Execute the required
  tool calls...]"`. Capped at 3 continuations.
- **Post-tool empty response nudge** — when model returns empty after tool
  execution, nudges once.
- **Budget grace call** — when budget exhausted mid-tool-use, grants one
  extra API call for the model to summarize.
- **Max iterations summary** — strips tools and asks model to summarize
  what it accomplished and what remains.
- **Error classification** — auth errors stop immediately, rate limits
  retry with backoff, same-tool-same-error after 2x blocks further retries.

## OpenAI Agents SDK

Source: `openai/openai-agents-python`

### Key Pattern

**Typed state machine**, not stop_reason branching:

```python
class SingleStepResult:
    next_step: NextStepHandoff | NextStepFinalOutput | NextStepRunAgain | NextStepInterruption
```

- `NextStepRunAgain` → continue loop
- `NextStepFinalOutput` → exit
- `NextStepHandoff` → swap agent, continue
- `NextStepInterruption` → pause for human

Tool failures → model-visible error strings, not exceptions.
Default `max_turns = 10`. No circuit breaker for tools.

## Vercel AI SDK

Source: `vercel/ai` packages

### Key Pattern

Does NOT use `finishReason` for continuation. Checks:
```typescript
continue = (clientToolCalls.length > 0 && allResolved) && !stopConditionMet
```

Text-only response (zero tool calls) always exits regardless of step count.
Default stop condition: `isStepCount(1)` (caller sets higher for agents).
No continuation prompts injected between steps.

## OpenClaw (Claude Code CLI, open-source)

Source: `openclaw/openclaw` — this IS the Claude Code CLI's open-source form.

### The Most Sophisticated Mid-Work Detection

OpenClaw detects THREE distinct failure modes and auto-nudges:

1. **Planning-only turns** (`resolvePlanningOnlyRetryInstruction`):
   Regex detection (`PLANNING_ONLY_PROMISE_RE`, `_HEADING_RE`, `_BULLET_RE`).
   Nudge: "The previous assistant turn only described the plan. Do not restate
   the plan. Act now: take the first concrete tool action you can."
   Limit: 1 normally, 2 in strict-agentic mode.

2. **Reasoning-only turns** (`resolveReasoningOnlyRetryInstruction`):
   Extended thinking produced but no visible user text.
   Nudge: "Continue from that partial turn and produce the visible answer now."
   Limit: 2 retries.

3. **Empty response turns** (`resolveEmptyResponseRetryInstruction`):
   Nudge: "Continue from the current state and produce the visible answer now."
   Limit: 1 retry.

4. **Ack execution fast-path**: When user says "do it" / "go ahead" (30+ phrases,
   10 languages), injects: "Start with the first concrete tool action immediately."

5. **Single-action-then-narrative**: Model makes one read-only tool call then writes
   a multi-step plan narrative. Treated as planning-only and nudged.

### Loop Detection (5 strategies, sliding window)

- `generic_repeat`: Same tool+params N times (warn at 10)
- `unknown_tool_repeat`: Nonexistent tool repeatedly (critical at 10)
- `known_poll_no_progress`: Polling with identical results (warn 10, critical 20)
- `global_circuit_breaker`: Any tool 30x with no progress change (blocks session)
- `ping_pong`: Alternating between two patterns (warn 10, critical 20)

Tool outcomes hashed (`sha256` of params + results) to detect "no progress".

## Recommended Architecture for Lynx

### Loop Structure (State Machine)

```python
iteration = 0
text_only_nudges = 0
max_tokens_retries = 0
started_tool_work = False
skill_error_streak: dict[str, tuple[str, int]] = {}

while True:
    # ── Budget guards ──────────────────────────────────────────
    if iteration >= MAX_TURNS:
        summarize_and_exit()
    if cumulative_tokens > TOKEN_BUDGET:
        compact_or_exit()

    response = anthropic.messages.create(...)
    iteration += 1
    cumulative_tokens += response.usage.input_tokens + response.usage.output_tokens

    # Append assistant message
    messages.append({"role": "assistant", "content": response.content})

    # Extract tool_use blocks from content (don't trust stop_reason)
    tool_calls = [b for b in response.content if b["type"] == "tool_use"]
    text = "".join(b["text"] for b in response.content if b["type"] == "text")

    # ── Branch on stop_reason ─────���────────────────────────────
    if response.stop_reason == "pause_turn":
        continue  # Server tools, resumable

    if tool_calls:
        # Execute tools, feed results back
        tool_results = execute_tools(tool_calls)
        messages.append({"role": "user", "content": tool_results})
        started_tool_work = True
        text_only_nudges = 0
        continue

    if response.stop_reason == "max_tokens":
        if max_tokens_retries >= 2:
            exit_incomplete("truncated")
        max_tokens_retries += 1
        messages.append({"role": "user", "content":
            "Continue from where you left off. Use tools if needed."})
        continue

    # ── Text-only response (end_turn) ──────────────────────────
    # Default terminal. But if we detect planning text after having
    # started tool work, nudge once (Hermes pattern).
    if started_tool_work and text_only_nudges < 2 and looks_like_planning(text):
        text_only_nudges += 1
        messages.append({"role": "user", "content":
            "Continue — execute your next step using tools."})
        continue

    # ── Final answer ───��───────────────────────────────────────
    return text
```

### Safety Defaults

| Limit | Value | Rationale |
|-------|-------|-----------|
| `MAX_TURNS` | 25 | Hard ceiling on model calls per user message |
| `TOKEN_BUDGET` | 150,000 | Soft ceiling; compact at 120K |
| `max_tokens_retries` | 2 | Retry truncation twice |
| `text_only_nudges` | 2 | Hermes uses 3; we cap at 2 |
| `max_elapsed_seconds` | 180 | Wall clock safety |
| Circuit breaker | 2 same errors | Auth/config errors stop immediately |

### System Prompt Reinforcement

Add to SKILL_USAGE_BLOCK:
```
CRITICAL: When your task requires more tool calls, ALWAYS include the next
tool call in your response. Never emit planning-only text without a tool call
when you still need external data. If you need to explain your approach, include
the explanation AND the tool call in the same response.
```

### Error Handling

- Tool errors → `tool_result` with error content, model decides next step
- Same skill same error 2x → inject STOP directive, don't retry
- Auth/credential errors → stop immediately (error class awareness)
- Rate limits / timeouts → retry with backoff up to 3x

### Token Budget & Compaction

Track cumulative tokens per turn. At 75% budget:
- Compact older messages (summarize)
- Preserve last 2 tool_use/tool_result pairs as few-shot examples
At 90% budget:
- Strip tools, ask model to summarize and stop

## What Our Current Loop Gets Wrong

1. **`stop_reason != "tool_use"` as exit** — should inspect content blocks instead
2. **`max_tokens` treated as terminal** — should retry with continuation prompt
3. **No `pause_turn` handling** — should continue
4. **Regex mid-work detection** — brittle; should be protocol-driven (content inspection + capped nudge)
5. **No token budget tracking** — context can blow up mid-turn
6. **`for` loop with iteration count** — should be `while True` with multi-dimensional guards
7. **One tool exception kills entire turn** — need per-tool isolation
8. **Circuit breaker is flat threshold** — need error class awareness
9. **Tool result cap is char-based** — should be token-estimated
10. **No max_tokens escalation** — should retry at higher output budget

## Migration Path

1. Replace `for iteration in range(MAX_LOOP)` with `while True` state machine
2. Branch on exact stop_reasons: `tool_use`, `end_turn`, `max_tokens`, `pause_turn`, `refusal`
3. Check content blocks for tool_use (not just stop_reason)
4. Add max_tokens recovery (retry with higher output cap)
5. Replace regex nudge with protocol-driven continuation (Hermes pattern)
6. Add cumulative token tracking with compaction triggers
7. Add per-tool error isolation
8. Upgrade circuit breaker with error classification
9. Reinforce system prompt: "never emit planning text without a tool call"

## References

- [Claude Code agent loop docs](https://code.claude.com/docs/en/agent-sdk/agent-loop)
- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [VILA-Lab paper: arXiv:2604.14228](https://arxiv.org/html/2604.14228v1)
- [liuup/claude-code-analysis](https://github.com/liuup/claude-code-analysis)
- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python)
- [Anthropic stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)
- [Vercel AI SDK agent loop](https://www.mintlify.com/vercel/ai/agents/loop-control)
