"""
Hermes Campaign Intelligence — normalized snapshots, trend analysis, anomaly detection.

Ingests campaign performance data from skills (Google Ads, Meta Ads),
normalizes metrics, detects trends and anomalies, and writes insights
to hermesMemories + CRM.
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger("hermes.campaign")


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_snapshot(raw: dict, platform: str) -> Optional[dict]:
    """
    Normalize campaign metrics from any platform into a standard schema.
    Returns a hermesCampaignSnapshots document.
    """
    metrics = raw.get("campaign_metrics") or raw.get("metrics") or {}
    if not metrics:
        return None

    return {
        "orgId": raw.get("orgId", ""),
        "customerId": raw.get("customerId", ""),
        "platform": platform,
        "campaignId": raw.get("campaign_id", ""),
        "campaignName": raw.get("campaign_name", ""),
        "date": raw.get("date", raw.get("captured_at", "")[:10]),
        "periodDays": raw.get("period_days", 7),
        "metrics": {
            "spend": _float(metrics.get("cost") or metrics.get("spend", 0)),
            "impressions": _int(metrics.get("impressions", 0)),
            "clicks": _int(metrics.get("clicks", 0)),
            "conversions": _float(metrics.get("conversions", 0)),
            "revenue": _float(metrics.get("revenue") or metrics.get("conversion_value", 0)),
            "ctr": _float(metrics.get("ctr", 0)),
            "cpc": _float(metrics.get("cpc") or metrics.get("avg_cpc", 0)),
            "cpa": _float(metrics.get("cpa") or metrics.get("cost_per_conversion", 0)),
            "roas": _float(metrics.get("roas", 0)),
        },
        "capturedAt": raw.get("captured_at", _now_iso()),
        "normalizedAt": _now_iso(),
    }


def detect_anomalies(current: dict, baseline: dict) -> list[dict]:
    """
    Compare current metrics against a baseline and detect anomalies.
    Returns a list of anomaly dicts.
    """
    anomalies = []
    cm = current.get("metrics", {})
    bm = baseline.get("metrics", {})

    rules = [
        {
            "name": "spend_up_conversions_flat",
            "check": lambda: (
                cm.get("spend", 0) > bm.get("spend", 0) * 1.2
                and cm.get("conversions", 0) <= bm.get("conversions", 0) * 1.05
            ),
            "severity": "high",
            "title": "Spend increased but conversions flat",
            "detail": lambda: (
                f"Spend: ${bm.get('spend', 0):.0f} → ${cm.get('spend', 0):.0f} "
                f"(+{((cm.get('spend', 0) / max(bm.get('spend', 0), 1)) - 1) * 100:.0f}%), "
                f"Conversions: {bm.get('conversions', 0):.0f} → {cm.get('conversions', 0):.0f}"
            ),
        },
        {
            "name": "roas_drop",
            "check": lambda: (
                bm.get("roas", 0) > 0
                and cm.get("roas", 0) < bm.get("roas", 0) * 0.7
            ),
            "severity": "high",
            "title": "ROAS dropped significantly",
            "detail": lambda: (
                f"ROAS: {bm.get('roas', 0):.1f}x → {cm.get('roas', 0):.1f}x "
                f"({((cm.get('roas', 0) / max(bm.get('roas', 0), 0.01)) - 1) * 100:.0f}%)"
            ),
        },
        {
            "name": "ctr_crash",
            "check": lambda: (
                bm.get("ctr", 0) > 0.5
                and cm.get("ctr", 0) < bm.get("ctr", 0) * 0.5
            ),
            "severity": "medium",
            "title": "CTR dropped by more than 50%",
            "detail": lambda: (
                f"CTR: {bm.get('ctr', 0):.1f}% → {cm.get('ctr', 0):.1f}%"
            ),
        },
        {
            "name": "cpa_spike",
            "check": lambda: (
                bm.get("cpa", 0) > 0
                and cm.get("cpa", 0) > bm.get("cpa", 0) * 1.5
            ),
            "severity": "medium",
            "title": "CPA spiked",
            "detail": lambda: (
                f"CPA: ${bm.get('cpa', 0):.2f} → ${cm.get('cpa', 0):.2f} "
                f"(+{((cm.get('cpa', 0) / max(bm.get('cpa', 0), 0.01)) - 1) * 100:.0f}%)"
            ),
        },
        {
            "name": "conversion_outage",
            "check": lambda: (
                bm.get("conversions", 0) > 5
                and cm.get("conversions", 0) == 0
                and cm.get("clicks", 0) > 10
            ),
            "severity": "critical",
            "title": "Zero conversions despite traffic",
            "detail": lambda: (
                f"Clicks: {cm.get('clicks', 0)}, Conversions: 0 "
                f"(baseline was {bm.get('conversions', 0):.0f})"
            ),
        },
    ]

    for rule in rules:
        try:
            if rule["check"]():
                anomalies.append({
                    "rule": rule["name"],
                    "severity": rule["severity"],
                    "title": rule["title"],
                    "detail": rule["detail"](),
                    "platform": current.get("platform", ""),
                    "campaignId": current.get("campaignId", ""),
                    "campaignName": current.get("campaignName", ""),
                })
        except (ZeroDivisionError, TypeError):
            continue

    return anomalies


def detect_trends(snapshots: list[dict]) -> list[dict]:
    """
    Detect trends from a time-ordered list of normalized snapshots.
    Expects snapshots sorted by date ascending.
    """
    if len(snapshots) < 2:
        return []

    trends = []

    first_half = snapshots[:len(snapshots) // 2]
    second_half = snapshots[len(snapshots) // 2:]

    for metric_key in ["spend", "conversions", "revenue", "ctr", "roas", "cpa"]:
        first_avg = _avg([s.get("metrics", {}).get(metric_key, 0) for s in first_half])
        second_avg = _avg([s.get("metrics", {}).get(metric_key, 0) for s in second_half])

        if first_avg == 0:
            continue

        change_pct = ((second_avg - first_avg) / first_avg) * 100

        if abs(change_pct) >= 15:
            direction = "up" if change_pct > 0 else "down"
            good_if_up = metric_key in ("conversions", "revenue", "roas", "ctr")
            is_good = (direction == "up") == good_if_up

            trends.append({
                "metric": metric_key,
                "direction": direction,
                "changePct": round(change_pct, 1),
                "firstAvg": round(first_avg, 2),
                "secondAvg": round(second_avg, 2),
                "isPositive": is_good,
                "period": f"{snapshots[0].get('date', '')} to {snapshots[-1].get('date', '')}",
            })

    return trends


def anomalies_to_memories(
    anomalies: list[dict],
    org_id: str,
    customer_id: str = "",
) -> list[dict]:
    """Convert detected anomalies into hermesMemories documents."""
    severity_to_importance = {"critical": 90, "high": 70, "medium": 50, "low": 30}

    memories = []
    for anomaly in anomalies:
        memories.append({
            "orgId": org_id,
            "scopeType": "customer" if customer_id else "org",
            "scopeId": customer_id or org_id,
            "memoryType": "campaign_anomaly",
            "title": anomaly["title"],
            "summary": anomaly["detail"],
            "importance": severity_to_importance.get(anomaly["severity"], 50),
            "confidence": 0.8,
            "relevanceTags": [
                "campaign", anomaly["platform"],
                anomaly["rule"], anomaly["severity"],
            ],
            "source": {
                "kind": "campaign_analysis",
                "campaignId": anomaly.get("campaignId", ""),
                "campaignName": anomaly.get("campaignName", ""),
            },
        })
    return memories


def trends_to_memories(
    trends: list[dict],
    org_id: str,
    platform: str = "",
    customer_id: str = "",
) -> list[dict]:
    """Convert detected trends into hermesMemories documents."""
    memories = []
    for trend in trends:
        emoji = "📈" if trend["isPositive"] else "📉"
        memories.append({
            "orgId": org_id,
            "scopeType": "customer" if customer_id else "org",
            "scopeId": customer_id or org_id,
            "memoryType": "campaign_insight",
            "title": f"{trend['metric']} trending {trend['direction']}",
            "summary": (
                f"{emoji} {trend['metric']} changed {trend['changePct']:+.1f}% "
                f"({trend['firstAvg']:.2f} → {trend['secondAvg']:.2f}) "
                f"over {trend['period']}"
            ),
            "importance": 40 if trend["isPositive"] else 60,
            "confidence": 0.7,
            "relevanceTags": ["campaign", "trend", trend["metric"], platform],
            "source": {"kind": "campaign_analysis"},
        })
    return memories


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _avg(values: list) -> float:
    nums = [v for v in values if v is not None]
    return sum(nums) / len(nums) if nums else 0.0
