---
title: "Chat Streaming Merge"
aliases: ["streaming-race", "firestore-listener", "chat-merge", "windowed-rendering"]
tags: [frontend, streaming, firestore, race-condition, ai-sdk, performance]
created: 2026-04-19
updated: 2026-04-19
status: active
---

# Chat Streaming Merge

How `components/chat/chat-interface.tsx` reconciles three writers to the `messages` array: useChat (AI SDK v6 streaming deltas), the Firestore `onSnapshot` listener (cross-user / cross-tab sync), and the localStorage cache. Designed to survive races without manual refresh.

## The race

Three writers, all calling `setMessages`:

1. **useChat** — appends text deltas by *mutating the message object in place*. The text part's `.text` string grows; tool parts mutate `state` from `input-streaming` → `input-available` → `output-available` with `output` filling in.
2. **Firestore `onSnapshot`** — fires on any write to `chatRooms/{id}/messages`, replaces incoming docs with fresh JS objects (different references).
3. **Debounced save** — POSTs the full local `messages` array to `/api/chat-rooms/{id}/messages` 2s after status flips to `"ready"`.

Naive merge (Firestore wins on id clash) breaks during streaming: the Firestore listener replaces useChat's message object with a fresh one, but useChat's *next text delta still mutates the orphaned old object* → UI freezes mid-stream until refresh.

Naive merge ALSO breaks just after stream-end: the post-stream Firestore snapshot can land before the debounced save persists the final assistant message. Firestore's lighter version (or older state) wins on the clash → assistant bubble goes blank.

## The two-tier protection

### Tier 1: streaming-status gate

```ts
const streamingStatusRef = useRef<string>("ready");
// updated synchronously each render: streamingStatusRef.current = status

unsubMessages = onSnapshot(msgsQuery, (snap) => {
  setMessages((prev) => {
    const isStreaming = streamingStatusRef.current === "streaming"
                     || streamingStatusRef.current === "submitted";
    if (isStreaming) {
      // ONLY add brand-new ids (cross-user writes); never replace existing.
      const localIds = new Set(prev.map((m) => m.id));
      const additions = incoming.filter((m) => m.id && !localIds.has(m.id));
      if (additions.length === 0) return prev;
      return [...prev, ...additions];
    }
    // Non-streaming path → tier 2
  });
});
```

During streaming, Firestore can't touch any id we already hold. Cross-user messages still flow through (their id is new to us).

### Tier 2: structural protection with grace window

Once status flips to `"ready"`, we still don't immediately trust Firestore over local for ids we touched recently:

```ts
const recentlyTouchedRef = useRef(new Map<string, number>());

// Updated whenever the messages array changes:
useEffect(() => {
  for (const m of messages) {
    if (m.id) recentlyTouchedRef.current.set(m.id, Date.now());
  }
}, [messages]);

// In the non-streaming merge:
const STRUCTURAL_GRACE_MS = 10_000;
const protectLocal =
  !!local && local.parts.length > inc.parts.length
  && Date.now() - touchedAt < STRUCTURAL_GRACE_MS;
```

Two conditions to keep local on id clash:
1. Local has more parts than incoming (incoming is structurally lighter).
2. We touched local within the last 10 seconds (it's recent enough to plausibly be ahead).

After 10s, the protection lifts and Firestore wins unconditionally — prevents stale local from pinning forever if the orchestrator legitimately compacts a message's parts. Map entries past 30s (3× grace) are GC'd inside the merge.

### Why parts.length and not byte-comparison

Earlier iterations tried byte-length comparison (`JSON.stringify(part).length`) so the heuristic would catch state transitions. Codex flagged real failure cases:
- AI SDK v6 `input-streaming` → `input-available`: same-length state strings.
- `approval-requested` → `output-denied`: pure state swap, same length.
- `addToolOutput` with `output-error`: replaces large `output` field with shorter `errorText`.

`parts.length` is structurally stable: useChat doesn't add or remove parts during normal mutation. So it's a reliable signal of "you have a part the snapshot is missing." If both sides have the same part count, we just trust Firestore — no per-part byte comparison.

## Windowed rendering

Separate concern, same file. `chat-message-list.tsx` only mounts the most recent `WINDOW_SIZE = 30` messages in the DOM. Older messages stay in the `messages` state array (so AI context, Firestore sync, localStorage, and data-action processing all see them) but aren't rendered. A "Load 30 earlier" pill appears at the top when there's hidden history.

This was the fix for keystroke lag at high message counts. With 100+ messages × markdown re-rendering on every parent re-render, the prior implementation made typing visibly stutter. Combined with:

- **Local input state**: `ChatInput` owns `input` as `useState`, parent never re-renders on keystroke.
- **`React.memo(ChatMessage)`**: with parts-length / last-part signature comparator. Prevents cascading re-renders from unrelated state changes.

See `commit bb76549` for the perf fix bundle.

### Tunable knobs (chat-message-list.tsx)
- `WINDOW_SIZE` (default 30) — initial visible message count.
- `WINDOW_STEP` (default 30) — rows revealed per "Load earlier" click.

Lowering to 10 / 10 would tighten the perf envelope but force more clicks to see history. The user has indicated they don't usually need historical messages — the click-to-reveal pattern matches that.

## Subtle gotchas

- **Object identity matters for useChat**: never replace an in-flight assistant message with a clone — the next stream delta will write to the orphaned reference and vanish. Tier 1 prevents this; tier 2 catches the immediate post-stream version.
- **Firestore listener fires on initial subscribe**: load arrives via `onSnapshot` even when nothing changed remotely. Tier 1 handles this safely (everything is "additions" because local was empty).
- **localStorage save is fast & synchronous**: not a race source. JSON.stringify on every messages change is bounded by `MAX_STORAGE_BYTES = 2MB`.

## Files

- `components/chat/chat-interface.tsx` — listener + merge + ref-based status mirror
- `components/chat/chat-message-list.tsx` — windowed render
- `components/chat/chat-message.tsx` — `React.memo` comparator (uses `partsSignature` for streaming text deltas)
- `components/chat/chat-input.tsx` — local input state

## Related

- [[lynx-quality-architecture]] — the broader chat performance + quality work
- [[ARCHITECTURE]] — Lynx system overview

## History

- `bb76549` (zeon) — perf fixes: input local + windowed render + memo
- `0824978` (zeon) — memo comparator fix for streaming text deltas
- `0d4b535` (zeon) — tier 1: status-gated listener
- `348a11e` (zeon) — tier 2: structural protection with 10s grace
