#!/usr/bin/env python3
"""
Backfill entityRefs + entityKeys on existing hermesMemories docs.

Idempotent: re-running only re-writes docs where current entityKeys differs
from newly-matched ones (taxonomy edits propagate).

Usage:
    cd orchestrator && python3 scripts/backfill_hermes_entities.py --dry-run
    cd orchestrator && python3 scripts/backfill_hermes_entities.py --org org_1758833352015
    cd orchestrator && python3 scripts/backfill_hermes_entities.py --all
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_store import _get_db  # noqa: E402
from hermes_entity_matcher import match_entities, entity_keys  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_entities")


def backfill(org_id: str | None, dry_run: bool, batch_size: int = 200):
    db = _get_db()
    if db is None:
        logger.error("Firestore unavailable")
        return 1

    query = db.collection("hermesMemories").where("status", "==", "active")
    if org_id:
        query = query.where("orgId", "==", org_id)

    total = 0
    changed = 0
    unchanged = 0
    by_type_changed: dict[str, int] = {}
    sample_keys: list[str] = []

    batch = db.batch()
    batch_writes = 0

    for doc in query.stream():
        data = doc.to_dict() or {}
        total += 1

        text = f"{data.get('title', '')} {data.get('summary', '')}".strip()
        # Pull skill_configs hints from source if present
        source = data.get("source") or {}
        skill_configs = source.get("skillConfigs") if isinstance(source, dict) else None
        refs = match_entities(
            text,
            org_id=data.get("orgId", ""),
            skill_configs=skill_configs,
        )
        new_keys = entity_keys(refs)

        existing_keys = data.get("entityKeys") or []
        if sorted(new_keys) == sorted(existing_keys):
            unchanged += 1
            continue

        changed += 1
        mt = data.get("memoryType", "unknown")
        by_type_changed[mt] = by_type_changed.get(mt, 0) + 1
        if len(sample_keys) < 5 and new_keys:
            sample_keys.append(f"{doc.id} ({mt}): {new_keys}")

        if dry_run:
            continue

        batch.update(doc.reference, {
            "entityRefs": refs,
            "entityKeys": new_keys,
        })
        batch_writes += 1
        if batch_writes >= batch_size:
            batch.commit()
            batch = db.batch()
            batch_writes = 0
            logger.info("Committed batch; scanned=%d changed=%d", total, changed)

    if batch_writes > 0 and not dry_run:
        batch.commit()

    logger.info("─" * 50)
    logger.info("scanned=%d changed=%d unchanged=%d%s", total, changed, unchanged,
                " (dry-run)" if dry_run else "")
    if by_type_changed:
        logger.info("changes by type: %s", by_type_changed)
    if sample_keys:
        logger.info("samples:")
        for s in sample_keys:
            logger.info("  %s", s)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--org", help="Backfill only this orgId")
    p.add_argument("--all", action="store_true", help="Backfill all orgs")
    p.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = p.parse_args()
    if not args.all and not args.org:
        p.error("specify --org <id> or --all")
    sys.exit(backfill(
        org_id=None if args.all else args.org,
        dry_run=args.dry_run,
    ))
