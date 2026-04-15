"""
Hermes turn classifier — lightweight, zero-cost classification of conversation turns.

Classifies each turn as trivial/decision/action_item/preference/campaign_change
using regex and keyword matching. No LLM calls.
"""

import re
from typing import Optional

DECISION_PATTERNS = [
    re.compile(r"\b(?:decided|decision|agreed|let'?s go with|we'?ll use|approved|confirmed)\b", re.I),
    re.compile(r"\b(?:we should|i want to|let'?s|go ahead|proceed with|switch to)\b", re.I),
    re.compile(r"\b(?:change .+ to|move forward|finalize|commit to)\b", re.I),
]

ACTION_PATTERNS = [
    re.compile(r"\b(?:todo|to-do|follow[- ]?up|action item|next step|reminder|need to|must|should)\b", re.I),
    re.compile(r"\b(?:by (?:monday|tuesday|wednesday|thursday|friday|tomorrow|end of|next week))\b", re.I),
    re.compile(r"\b(?:assign|schedule|set up|create a|file a|open a)\b", re.I),
]

PREFERENCE_PATTERNS = [
    re.compile(r"\b(?:i (?:prefer|like|want|always|never|don'?t like))\b", re.I),
    re.compile(r"\b(?:please (?:always|never|don'?t))\b", re.I),
    re.compile(r"\b(?:my style|my preference|i usually|i tend to)\b", re.I),
    re.compile(r"\b(?:from now on|going forward|in the future)\b", re.I),
]

CAMPAIGN_PATTERNS = [
    re.compile(r"\b(?:budget|bid|bidding|spend|roas|cpa|cpc|ctr|conversion|impression)\b", re.I),
    re.compile(r"\b(?:campaign|ad group|ad set|keyword|targeting|audience)\b", re.I),
    re.compile(r"\b(?:pause|enable|increase|decrease|optimize|scale)\b", re.I),
]

GREETING_PATTERNS = [
    re.compile(r"^(?:hi|hello|hey|good (?:morning|afternoon|evening)|thanks|thank you|ok|okay|got it|sure|yes|no|yep|nope)\s*[.!?]*$", re.I),
]


class TurnClassification:
    __slots__ = ("is_trivial", "has_decision", "has_action_item",
                 "has_preference", "has_campaign_change", "importance")

    def __init__(self):
        self.is_trivial = True
        self.has_decision = False
        self.has_action_item = False
        self.has_preference = False
        self.has_campaign_change = False
        self.importance = 0

    def to_dict(self) -> dict:
        return {
            "is_trivial": self.is_trivial,
            "has_decision": self.has_decision,
            "has_action_item": self.has_action_item,
            "has_preference": self.has_preference,
            "has_campaign_change": self.has_campaign_change,
            "importance": self.importance,
        }


def classify_turn(text: str, role: str = "user", tool_names: Optional[list[str]] = None) -> TurnClassification:
    """Classify a conversation turn using cheap regex/keyword matching."""
    result = TurnClassification()

    if not text or len(text.strip()) < 3:
        return result

    text = text.strip()

    for pat in GREETING_PATTERNS:
        if pat.match(text):
            return result

    result.is_trivial = False

    for pat in DECISION_PATTERNS:
        if pat.search(text):
            result.has_decision = True
            result.importance += 30
            break

    for pat in ACTION_PATTERNS:
        if pat.search(text):
            result.has_action_item = True
            result.importance += 25
            break

    for pat in PREFERENCE_PATTERNS:
        if pat.search(text):
            result.has_preference = True
            result.importance += 20
            break

    for pat in CAMPAIGN_PATTERNS:
        if pat.search(text):
            result.has_campaign_change = True
            result.importance += 15
            break

    if tool_names:
        result.importance += min(len(tool_names) * 10, 30)

    if len(text) > 200:
        result.importance += 10

    if role == "assistant" and len(text) > 500:
        result.importance += 10

    result.importance = min(result.importance, 100)

    if result.importance == 0 and not result.is_trivial:
        result.importance = 5

    return result
