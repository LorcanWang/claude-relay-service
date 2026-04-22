#!/usr/bin/env python3
"""
Aggregate agent_metrics.ndjson into a quick console summary.

Usage:
    python3 scripts/agent_metrics_summary.py
    python3 scripts/agent_metrics_summary.py --since 24h
    python3 scripts/agent_metrics_summary.py --path /custom/agent_metrics.ndjson

Reads the JSONL file emitted by orchestrator/metrics_emitter.py (one
record per /chat turn) and prints:
  - total turns
  - outcome distribution (success/error/cancelled/max_loop_exhausted)
  - avg tool_call_count, retrieval_hit_count, duration_ms
  - retrieval_empty rate
  - per-room outcome breakdown

This is intentionally a read-only tail-and-aggregate. No DB, no Firestore
write, no LLM calls. When usage signal demands more, point a real
dashboard (Langfuse, Phoenix) at the same file.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent.parent.parent / "logs" / "agent_metrics.ndjson"


def parse_since(spec: str) -> timedelta | None:
    """Parse simple duration specs: `24h`, `7d`, `60m`. None → no filter."""
    if not spec:
        return None
    try:
        unit = spec[-1].lower()
        n = int(spec[:-1])
    except (ValueError, IndexError):
        return None
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "m":
        return timedelta(minutes=n)
    return None


def load_records(path: Path, since: timedelta | None) -> list[dict]:
    if not path.exists():
        print(f"No metrics file at {path}")
        return []
    cutoff = datetime.now(timezone.utc) - since if since else None
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff:
                ts = rec.get("ts", "")
                try:
                    if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    continue
            records.append(rec)
    return records


def fmt_pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.1f}%" if total else "—"


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize agent_metrics.ndjson")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--since", type=str, default="", help="e.g. 24h, 7d, 60m")
    args = parser.parse_args()

    since = parse_since(args.since)
    recs = load_records(args.path, since)
    if not recs:
        print(f"No records in {args.path}" + (f" since {args.since}" if args.since else ""))
        return 0

    total = len(recs)
    outcome_counts: Counter[str] = Counter()
    tool_calls = 0
    retrieval_hits = 0
    retrieval_empty_count = 0
    duration_total = 0
    duration_n = 0
    by_room: dict[str, Counter[str]] = defaultdict(Counter)

    for r in recs:
        m = r.get("metrics", {})
        outcome = m.get("final_outcome", "unknown")
        outcome_counts[outcome] += 1
        tool_calls += int(m.get("tool_call_count", 0) or 0)
        retrieval_hits += int(m.get("retrieval_hit_count", 0) or 0)
        if m.get("retrieval_empty"):
            retrieval_empty_count += 1
        d = m.get("duration_ms")
        if isinstance(d, (int, float)):
            duration_total += int(d)
            duration_n += 1
        room = r.get("room_id") or "(no room)"
        by_room[room][outcome] += 1

    title = f"Agent metrics — {total} turns" + (f" (last {args.since})" if args.since else "")
    print(title)
    print("=" * len(title))
    print()
    print("Outcome distribution:")
    for outcome, n in outcome_counts.most_common():
        print(f"  {outcome:25} {n:6}  {fmt_pct(n, total):>6}")
    print()
    print(f"avg tool_call_count        : {tool_calls / total:.2f}")
    print(f"avg retrieval_hit_count    : {retrieval_hits / total:.2f}")
    print(f"retrieval_empty rate       : {fmt_pct(retrieval_empty_count, total)}")
    if duration_n:
        print(f"avg duration_ms (n={duration_n:>4}): {duration_total / duration_n:.0f}")
    print()
    if len(by_room) > 1:
        print("Per-room outcomes:")
        for room, counts in sorted(by_room.items(), key=lambda x: -sum(x[1].values()))[:20]:
            line = ", ".join(f"{k}={v}" for k, v in counts.most_common())
            print(f"  {room[:36]:36} {sum(counts.values()):4}  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
