"""
Hermes entity matcher — deterministic extraction of entity references
(channel, platform, product_line, sku, campaign, customer) from memory text
for cross-room memory bridging.

Taxonomy resolution order:
  1. hermesEntityTaxonomy/{orgId} — per-org override
  2. hermesEntityTaxonomy/_global — platform default
  3. Hardcoded fallback (DEFAULT_TAXONOMY below)

Output shape:
  entityRefs: [{kind, id, label, source}]   — structured
  entityKeys: ["kind:id", ...]               — flat, indexed for array_contains_any
"""
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("hermes.entity_matcher")

# Hardcoded fallback — used only when Firestore taxonomy doc is unavailable.
# The _global doc in hermesEntityTaxonomy mirrors and overrides these at runtime.
DEFAULT_TAXONOMY = {
    "channels": [
        "amazon", "wayfair", "shopify", "homedepot", "ebay", "etsy",
        "walmart", "bigcommerce", "tiktok_shop",
    ],
    "platforms": [
        "amazon_ads", "meta_ads", "google_ads", "ga4", "tiktok_ads",
        "google_analytics", "google_search_console",
    ],
    "productLines": [
        # seeded per-org; _global stays empty
    ],
    "skuPattern": r"^[A-Z]{2,6}-\d{2,5}$",
    # Map SKU prefix -> product_line slug so "CAB-087 delayed" tags product_line:cabinet
    # without requiring the word "cabinet" in the message.
    # { "CAB": "cabinet", "PIL": "pillow" }
    "skuPrefixToProductLine": {},
}

_TAXONOMY_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 60


def _fetch_taxonomy_doc(db, doc_id: str) -> Optional[dict]:
    if not doc_id:
        return None
    try:
        doc = db.collection("hermesEntityTaxonomy").document(doc_id).get()
        if doc.exists:
            return doc.to_dict() or {}
    except Exception as exc:
        logger.warning("Failed to read taxonomy %r: %s", doc_id, exc)
    return None


def load_taxonomy(org_id: str) -> dict:
    """Return taxonomy merging: DEFAULT < _global < per-org override.

    Values from later sources overwrite earlier ones. List fields are replaced,
    not merged — if org wants to add channels they must include the full list.
    """
    now = time.monotonic()
    cached = _TAXONOMY_CACHE.get(org_id)
    if cached and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]

    merged = dict(DEFAULT_TAXONOMY)

    # Late import to avoid circular with hermes_store at module load.
    try:
        from hermes_store import _get_db
        db = _get_db()
    except Exception:
        db = None

    if db is not None:
        for doc_id in ("_global", org_id):
            if not doc_id:
                # Skip empty org_id so we never hit "hermesEntityTaxonomy/" (trailing slash).
                continue
            doc = _fetch_taxonomy_doc(db, doc_id)
            if not doc:
                continue
            for key in ("channels", "platforms", "productLines"):
                if key in doc and isinstance(doc[key], list):
                    merged[key] = doc[key]
            if isinstance(doc.get("skuPattern"), str):
                merged["skuPattern"] = doc["skuPattern"]
            if isinstance(doc.get("skuPrefixToProductLine"), dict):
                merged["skuPrefixToProductLine"] = doc["skuPrefixToProductLine"]

    _TAXONOMY_CACHE[org_id] = (merged, now)
    return merged


def clear_taxonomy_cache():
    """Test hook — force re-read on next call."""
    _TAXONOMY_CACHE.clear()


# ── Matching ────────────────────────────────────────────────────────────────

_WORD_SPLIT = re.compile(r"[^a-zA-Z0-9_]+")
_SKU_RE_CACHE: dict[str, re.Pattern] = {}


def _sku_regex(pattern: str) -> re.Pattern:
    cached = _SKU_RE_CACHE.get(pattern)
    if cached:
        return cached
    try:
        compiled = re.compile(pattern)
    except re.error:
        compiled = re.compile(DEFAULT_TAXONOMY["skuPattern"])
    _SKU_RE_CACHE[pattern] = compiled
    return compiled


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD_SPLIT.split(text or "") if t]


def _match_set(tokens_lower: set[str], vocabulary: list[str], kind: str) -> list[dict]:
    """Each vocab entry can be multi-word (e.g. 'home depot'); match on token set."""
    out = []
    seen = set()
    text_lower = " ".join(sorted(tokens_lower))  # for multi-word substring hits
    for entry in vocabulary:
        slug = str(entry).strip().lower()
        if not slug or slug in seen:
            continue
        entry_tokens = [t for t in _WORD_SPLIT.split(slug) if t]
        if not entry_tokens:
            continue
        if all(t in tokens_lower for t in entry_tokens):
            seen.add(slug)
            out.append({
                "kind": kind,
                "id": slug.replace(" ", "_"),
                "label": entry,
                "source": "deterministic",
            })
    return out


def _match_skus(text: str, pattern: str) -> list[dict]:
    if not text or not pattern:
        return []
    out = []
    seen = set()
    # Extract candidates (bare tokens that match the SKU shape).
    for candidate in re.findall(r"[A-Z]{2,6}-\d{2,5}", text):
        if candidate in seen:
            continue
        if _sku_regex(pattern).match(candidate):
            seen.add(candidate)
            out.append({
                "kind": "sku",
                "id": candidate,
                "label": candidate,
                "source": "deterministic",
            })
    return out


def match_entities(
    text: str,
    org_id: str,
    skill_configs: Optional[dict] = None,
) -> list[dict]:
    """Extract entity references from text deterministically.

    `skill_configs` is the per-room map passed into the chat request — we pull
    customer/campaign IDs from it (crm-notes.customer_id,
    google-ad-campaign.customer_id).
    """
    if not text:
        return []

    taxonomy = load_taxonomy(org_id)
    tokens = _tokens(text)
    tokens_lower = {t.lower() for t in tokens}

    refs: list[dict] = []
    refs.extend(_match_set(tokens_lower, taxonomy.get("channels", []), "channel"))
    refs.extend(_match_set(tokens_lower, taxonomy.get("platforms", []), "platform"))
    refs.extend(_match_set(tokens_lower, taxonomy.get("productLines", []), "product_line"))

    sku_refs = _match_skus(text, taxonomy.get("skuPattern", DEFAULT_TAXONOMY["skuPattern"]))
    refs.extend(sku_refs)

    # Infer product_line from SKU prefix (e.g. CAB-087 -> product_line:cabinet)
    prefix_map = taxonomy.get("skuPrefixToProductLine") or {}
    for sku_ref in sku_refs:
        sku_id = sku_ref["id"]
        prefix = sku_id.split("-", 1)[0] if "-" in sku_id else ""
        line = prefix_map.get(prefix) or prefix_map.get(prefix.upper())
        if line:
            refs.append({
                "kind": "product_line",
                "id": str(line).lower(),
                "label": str(line).title(),
                "source": "sku_prefix",
            })

    if skill_configs:
        crm = (skill_configs.get("crm-notes") or {}).get("customer_id")
        if crm:
            refs.append({
                "kind": "customer",
                "id": str(crm),
                "label": str(crm),
                "source": "skill_config",
            })
        gads = (skill_configs.get("google-ad-campaign") or {}).get("customer_id")
        if gads:
            refs.append({
                "kind": "campaign",
                "id": f"google:{gads}",
                "label": f"Google Ads {gads}",
                "source": "skill_config",
            })

    # Dedupe by (kind, id)
    deduped: dict[tuple, dict] = {}
    for ref in refs:
        key = (ref["kind"], ref["id"])
        if key not in deduped:
            deduped[key] = ref
    return list(deduped.values())


def entity_keys(refs: list[dict]) -> list[str]:
    """Flatten entityRefs to the `kind:id` shape used for Firestore
    array_contains_any queries."""
    return [f"{r['kind']}:{r['id']}" for r in refs if r.get("kind") and r.get("id")]
