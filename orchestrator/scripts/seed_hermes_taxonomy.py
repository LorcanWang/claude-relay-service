#!/usr/bin/env python3
"""
Seed the `hermesEntityTaxonomy/_global` Firestore doc with platform-wide
entity vocabulary for cross-room memory bridging.

Idempotent: re-running overwrites the _global doc but never touches per-org
overrides (hermesEntityTaxonomy/{orgId} docs).

Usage:
    cd orchestrator && python3 scripts/seed_hermes_taxonomy.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_store import _get_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_taxonomy")


GLOBAL_TAXONOMY = {
    "channels": [
        "amazon",
        "wayfair",
        "shopify",
        "homedepot",
        "ebay",
        "etsy",
        "walmart",
        "bigcommerce",
        "tiktok_shop",
    ],
    "platforms": [
        "amazon_ads",
        "meta_ads",
        "google_ads",
        "ga4",
        "tiktok_ads",
        "google_analytics",
        "google_search_console",
    ],
    # productLines are populated per-org via admin UI. _global stays empty so
    # one org's banner SKUs don't pollute another org's cabinet graph.
    "productLines": [],
    "skuPattern": r"^[A-Z]{2,6}-\d{2,5}$",
    "skuPrefixToProductLine": {},
    "description": (
        "Platform-global Hermes entity taxonomy. Per-org overrides live at "
        "hermesEntityTaxonomy/{orgId}. Admin UI at /admin/memory-taxonomy."
    ),
}


def main():
    db = _get_db()
    if db is None:
        logger.error("Firestore unavailable — cannot seed taxonomy")
        return 1

    ref = db.collection("hermesEntityTaxonomy").document("_global")
    existing = ref.get()
    if existing.exists:
        logger.info("_global taxonomy already exists; overwriting to bring it in sync")
    ref.set(GLOBAL_TAXONOMY)
    logger.info(
        "Seeded hermesEntityTaxonomy/_global: %d channels, %d platforms",
        len(GLOBAL_TAXONOMY["channels"]),
        len(GLOBAL_TAXONOMY["platforms"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
