"""
Per-turn agent metrics — append-only JSONL on disk.

One line per /chat turn at `logs/agent_metrics.ndjson`. Designed as a
stable event envelope so we can later ingest into Firestore / Langfuse /
OpenTelemetry without changing call sites. The shape mirrors the
OpenTelemetry GenAI semantic-conventions vocabulary loosely (run_id,
metrics block) so promotion to OTEL spans later is mechanical.

Why JSONL on disk and not Firestore yet:
  - Lowest operational complexity for a single-developer no-user product.
  - Inspectable with `tail -f logs/agent_metrics.ndjson | jq`.
  - No Firestore schema churn while the event shape settles.
  - Works offline / during local development.
  - Replayable into Firestore or Langfuse OTEL ingester later.

A single uvicorn process can't interleave appends because each emit is
synchronous (no `await` during the write) and the asyncio loop yields
only at await points. Multi-worker uvicorn would need a lock or a queue
— deferred until we actually flip multi-worker on (gated on the
`status_hub` Redis pub/sub work in [[skill-concurrency]]).

Disable via `AGENT_METRICS_ENABLED=0` for load-shedding or tests.
Point elsewhere via `AGENT_METRICS_PATH=/some/other/path.ndjson`.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("metrics")

# Default to logs/ next to orchestrator/, matching the rest of the boot scripts.
_DEFAULT_PATH = Path(__file__).parent.parent / "logs" / "agent_metrics.ndjson"
METRICS_PATH = Path(os.environ.get("AGENT_METRICS_PATH", str(_DEFAULT_PATH)))
ENABLED = os.environ.get("AGENT_METRICS_ENABLED", "1") != "0"

# Allowed values for the final_outcome enum. Kept narrow on purpose — these
# are the only states the /chat handler can honestly report. Add new values
# only when there's a concrete state to report from a real call site.
OUTCOMES = {"success", "error", "cancelled", "max_loop_exhausted", "room_busy"}


def new_run_id() -> str:
    """Caller mints a run_id at the start of /chat so the record correlates
    with whatever it logs along the way."""
    return uuid.uuid4().hex


FIRESTORE_COLLECTION = "agentMetrics"
# Disable Firestore tail with AGENT_METRICS_FIRESTORE=0. JSONL stays the
# durable record either way — Firestore is the queryable mirror.
FIRESTORE_ENABLED = os.environ.get("AGENT_METRICS_FIRESTORE", "1") != "0"


def _write_firestore(record: dict) -> None:
    """Best-effort dual-write to Firestore so the zeon admin UI can read the
    same data the JSONL captures. Imports are deferred so the module loads
    even when firebase_admin / hermes_store aren't initialized (tests, CLI
    runs of the aggregator)."""
    if not FIRESTORE_ENABLED:
        return
    try:
        from hermes_store import _get_db
    except Exception:
        return
    db = _get_db()
    if db is None:
        return
    try:
        # Use the run_id as the doc id so retries / replays can be made
        # idempotent later. Today retries don't happen (one emit per turn),
        # so set() is fine.
        db.collection(FIRESTORE_COLLECTION).document(record["run_id"]).set(record)
    except Exception as exc:
        logger.debug("metrics_emitter: firestore write failed: %s", exc)


def emit_run_completed(
    *,
    run_id: str,
    session_id: str | None = None,
    room_id: str | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    tool_call_count: int = 0,
    retrieval_hit_count: int = 0,
    retrieval_empty: bool = True,
    final_outcome: str = "success",
    duration_ms: int | None = None,
    extra: dict | None = None,
) -> None:
    """Append one per-turn record to JSONL + dual-write to Firestore.
    Best-effort on both paths; never raises to the caller — this must
    never break /chat.

    final_outcome should be one of `OUTCOMES`. Unknown values are accepted
    and recorded as-is (we log a warning) so a future call-site can extend
    the enum without coordinating with this module.
    """
    if not ENABLED:
        return
    if final_outcome not in OUTCOMES:
        logger.warning("metrics_emitter: unknown final_outcome=%r", final_outcome)
    record: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": "agent_run_completed",
        "run_id": run_id,
        "session_id": session_id,
        "room_id": room_id,
        "org_id": org_id,
        "user_id": user_id,
        "metrics": {
            "tool_call_count": tool_call_count,
            "retrieval_hit_count": retrieval_hit_count,
            "retrieval_empty": retrieval_empty,
            "final_outcome": final_outcome,
        },
    }
    if duration_ms is not None:
        record["metrics"]["duration_ms"] = duration_ms
    if extra:
        record["extra"] = extra
    try:
        METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        # Append failures should not propagate. The /chat handler MUST stay
        # functional even if the metrics file is unwritable (e.g. disk full,
        # rotated mid-flight).
        logger.debug("metrics_emitter: append failed: %s", exc)
    _write_firestore(record)
