---
title: "Tool Result Envelope"
aliases: ["envelope", "tool-result", "tool-envelope"]
tags: [orchestrator, tool-use, observability, claude]
created: 2026-04-19
updated: 2026-04-19
status: active
---

# Tool Result Envelope

Every tool result Claude sees — whether from `run_command`, `describe_skill`, `app_action`, or an MCP tool — passes through a single helper (`_build_tool_envelope` in `orchestrator/main.py`) that produces a uniform JSON shape. Standardized so Claude scans the outcome before parsing data, and so failures preserve the diagnostic signal.

## Shape

```json
{
  "status": "ok" | "error",
  "summary": "ran ga4 (python3 ga4.py revenue) — 3 items",
  "data":   <result, clipped to 18k chars when necessary>,
  "error":  "USER_PERMISSION_DENIED",
  "stderr": "<last 2k of stderr>",
  "stdout": "<last 2k of stdout>",
  "meta": {
    "truncated": true,
    "agentNote": "...",
    "action": "list_campaigns",
    "action_gap": true,
    "dropped_data": true
  }
}
```

Insertion order matters — Claude's eye lands on `status` and `summary` first when scanning a JSON envelope. Heavy fields (`data`, `stderr`, `stdout`) sit lower so the model doesn't have to hunt for the verdict.

## Field semantics

### Always present
- `status`: `"ok"` or `"error"`. Drives Hermes `result_ok` and downstream confirmation flows.
- `summary`: short human-readable outcome (see "Per-tool summaries" below).

### Success-only
- `data`: actual tool result. Strings stay strings. Dicts/lists JSON-encoded inline. Clipped at `TOOL_RESULT_MAX_DATA = 18000` chars; truncation flagged via `meta.truncated`.

### Failure-only
- `error`: clipped at 1000 chars (pathological errors can't blow the envelope budget).
- `stderr` / `stdout`: each clipped to last 2000 chars. Tail-clipping preserves the most recent diagnostic output.

### Meta
- `truncated`: set when `data` was clipped to fit `TOOL_RESULT_MAX_DATA`.
- `agentNote`: small explanation strings emitted by skills via `result["agentNote"]`. Captured even on failed MCP results.
- `action`: matched action id from the skill's [[skill-manifest-evolution#actions-schema|actions[] manifest]] — phase 2 validation.
- `action_gap`: true when the skill has actions[] declared but the model's command didn't match any. Burn-in observability signal.
- `dropped_data` / `dropped_stderr` / `dropped_stdout`: set when the over-budget pruner had to drop a field.

## Per-tool summaries

The summary is generated based on `tool_name` and the result shape:

| Tool | Success | Failure |
|---|---|---|
| `run_command` | `ran <skill> (<cmd-prefix>) — <data-shape>` | `<skill> failed: <error[:140]>` |
| `describe_skill` | `loaded <name> docs (N chars)` | `describe_skill failed: <error>` |
| `app_action` | `queued <action> <path?>` | `app_action failed: <error>` |
| MCP tool | `<tool>: <data-shape>` | `<tool> failed: <error>` |

Data-shape is computed by `_summarize_data`:
- `null` → `"no data"`
- string → first line, clipped at 140 chars
- list → `"N items"`
- dict → `"keys: a, b, c, ..."` (first 6)
- int/float/bool → str

## Wrapper-key strip

Skills returning bare success dicts (no explicit `data` field) get their wrapper keys stripped before serialization:

```python
_wrapper = {"ok", "agentNote", "stderr", "stdout", "error"}
stripped = {k: v for k, v in raw.items() if k not in _wrapper}
data = stripped if stripped else None
```

So `{ok: True, campaigns: [...]}` becomes `data: {campaigns: [...]}` — Claude doesn't see redundant `ok: true` echoed inside data.

`app_action` results have data set to `None` entirely — the action was already collected client-side via `collected_actions`; echoing it back is noise.

## Over-budget pruning

If the serialized envelope exceeds `TOOL_RESULT_MAX_TOTAL = 20000` chars, `_envelope_to_tool_content` drops fields in priority order:

1. `stdout` (least diagnostic value if main outcome is in `summary` + `error`)
2. `stderr`
3. `data` (last resort — model can re-fetch via another tool call)

Each drop is recorded with `meta.dropped_<field>: true` so Claude knows what's missing. Never slices the JSON string itself — that produces invalid JSON that Claude can't parse.

## Hermes integration

`emit_tool_executed` consumes `envelope["summary"][:200]` as `result_summary` instead of `str(result)[:200]`. Cleaner one-line summaries flow into `hermesMemories` of type `campaign_insight` when the worker extracts patterns from tool runs. See [[hermes-memory-system]].

## Files

- `orchestrator/main.py:261` — `_build_tool_envelope` definition
- `orchestrator/main.py:363` — `_envelope_to_tool_content` (over-budget pruning)

## Related

- [[skill-manifest-evolution]] — how `meta.action` is populated
- [[lynx-quality-architecture]] — how envelope changes fit the broader quality plan
- [[hermes-memory-system]] — downstream consumer

## History

- `b78781c` — initial standardization (4 review iterations under Codex)
- `0c430aa` — added `matched_action` / `action_gap` kwargs for phase 2 validation
