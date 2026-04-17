"""
Unit tests for hermes_entity_matcher.

Run:
    cd orchestrator && python3 -m pytest tests/test_hermes_entity_matcher.py -v

These are pure-Python unit tests — no Firestore required. The taxonomy loader
is stubbed to return a fixed dict so tests don't depend on the network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import hermes_entity_matcher as em  # noqa: E402


FIXTURE_TAXONOMY = {
    "channels": ["amazon", "wayfair", "home depot"],
    "platforms": ["amazon_ads", "meta_ads", "google_ads", "ga4"],
    "productLines": ["cabinet", "pillow", "banner"],
    "skuPattern": r"^[A-Z]{2,6}-\d{2,5}$",
    "skuPrefixToProductLine": {"CAB": "cabinet", "PIL": "pillow", "BAN": "banner"},
}


def _stub_taxonomy(monkeypatch=None):
    """Replace load_taxonomy with a fixed fixture without needing Firestore."""
    em.clear_taxonomy_cache()
    em.load_taxonomy = lambda org_id: FIXTURE_TAXONOMY  # type: ignore


def setup_function(fn):
    _stub_taxonomy()


# ── Channel matching ────────────────────────────────────────────────────────

def test_matches_single_channel():
    refs = em.match_entities("Amazon sales dropped 20% this weekend", org_id="o1")
    kinds = {(r["kind"], r["id"]) for r in refs}
    assert ("channel", "amazon") in kinds


def test_matches_multi_word_channel():
    refs = em.match_entities("Home Depot sales broke records", org_id="o1")
    assert ("channel", "home_depot") in {(r["kind"], r["id"]) for r in refs}


def test_no_channel_in_text():
    refs = em.match_entities("Random prose with no entities.", org_id="o1")
    assert not any(r["kind"] == "channel" for r in refs)


# ── Platform vs channel separation ─────────────────────────────────────────

def test_amazon_ads_distinct_from_amazon():
    refs = em.match_entities("amazon_ads ACoS spiked on the amazon channel", org_id="o1")
    kinds = {(r["kind"], r["id"]) for r in refs}
    assert ("platform", "amazon_ads") in kinds
    assert ("channel", "amazon") in kinds


# ── Product line matching ──────────────────────────────────────────────────

def test_product_line():
    refs = em.match_entities("Cabinet inventory was short this week.", org_id="o1")
    assert ("product_line", "cabinet") in {(r["kind"], r["id"]) for r in refs}


def test_product_line_case_insensitive():
    refs = em.match_entities("BANNER reprints have quality issues.", org_id="o1")
    assert ("product_line", "banner") in {(r["kind"], r["id"]) for r in refs}


# ── SKU matching ───────────────────────────────────────────────────────────

def test_sku_match_basic():
    refs = em.match_entities("CAB-087 factory 5d late", org_id="o1")
    skus = [r for r in refs if r["kind"] == "sku"]
    assert len(skus) == 1
    assert skus[0]["id"] == "CAB-087"


def test_sku_multiple_distinct():
    refs = em.match_entities("CAB-087 delayed; PIL-12 backordered; CAB-087 duplicate", org_id="o1")
    skus = sorted(r["id"] for r in refs if r["kind"] == "sku")
    assert skus == ["CAB-087", "PIL-12"]  # deduped


def test_sku_rejects_malformed():
    # lowercase shouldn't match; pure digits shouldn't match; too-long shouldn't match
    refs = em.match_entities("cab-087 and 12345 and CABINET-12345678 are bad", org_id="o1")
    skus = [r for r in refs if r["kind"] == "sku"]
    assert skus == []


# ── Skill config resolution ────────────────────────────────────────────────

def test_customer_from_skill_config():
    skill_configs = {"crm-notes": {"customer_id": "cust_RlzjuJd5zb0Rn3S7MMpN"}}
    refs = em.match_entities(
        "Had a call with the customer about Q2",
        org_id="o1",
        skill_configs=skill_configs,
    )
    assert ("customer", "cust_RlzjuJd5zb0Rn3S7MMpN") in {(r["kind"], r["id"]) for r in refs}


def test_campaign_from_skill_config():
    skill_configs = {"google-ad-campaign": {"customer_id": "5978025978"}}
    refs = em.match_entities(
        "Checked Google Ads spend",
        org_id="o1",
        skill_configs=skill_configs,
    )
    assert ("campaign", "google:5978025978") in {(r["kind"], r["id"]) for r in refs}


# ── Composite / scenario tests (map to the 4-scenario matrix) ─────────────

def test_scenario_1_supply_shock():
    """Cabinet room: 'CAB-087 factory 5d late' — must tag SKU + product_line."""
    refs = em.match_entities("CAB-087 factory 5d late, 200u backlog", org_id="o1")
    keys = set(em.entity_keys(refs))
    assert "sku:CAB-087" in keys
    assert "product_line:cabinet" in keys


def test_scenario_2_ad_spike():
    """Amazon room: 'Cabinet ACoS +40%' — must tag product_line + channel + platform."""
    refs = em.match_entities(
        "Cabinet ACoS +40% this week on amazon, amazon_ads bidding too aggressive",
        org_id="o1",
    )
    keys = set(em.entity_keys(refs))
    assert "product_line:cabinet" in keys
    assert "channel:amazon" in keys
    assert "platform:amazon_ads" in keys


def test_scenario_3_unrelated_product():
    """Amazon room: 'Pillow CPA -30%' — tags pillow, NOT cabinet."""
    refs = em.match_entities(
        "Pillow CPA -30% after creative refresh on amazon", org_id="o1"
    )
    keys = set(em.entity_keys(refs))
    assert "product_line:pillow" in keys
    assert "product_line:cabinet" not in keys


def test_scenario_4_wayfair_exclusive():
    """Cabinet room: 'CAB-999 Wayfair-only factory delay' — tags wayfair, NOT amazon."""
    refs = em.match_entities(
        "CAB-999 is Wayfair-exclusive, factory delay there", org_id="o1"
    )
    keys = set(em.entity_keys(refs))
    assert "channel:wayfair" in keys
    assert "channel:amazon" not in keys
    assert "sku:CAB-999" in keys


# ── Edge cases ─────────────────────────────────────────────────────────────

def test_empty_text():
    assert em.match_entities("", org_id="o1") == []


def test_no_taxonomy_match_returns_empty():
    refs = em.match_entities("lorem ipsum dolor sit amet", org_id="o1")
    assert refs == []


def test_entity_keys_format():
    refs = [
        {"kind": "channel", "id": "amazon", "label": "Amazon", "source": "deterministic"},
        {"kind": "sku", "id": "CAB-087", "label": "CAB-087", "source": "deterministic"},
    ]
    assert em.entity_keys(refs) == ["channel:amazon", "sku:CAB-087"]
