"""Keira succession / exit signal tiers — family-owned ≠ exit intent."""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Tier 0 = context only (not exit evidence)
TIER0 = (
    (r"\bfamily[- ]owned\b", "family_owned"),
    (r"\bfamily business\b", "family_business"),
    (r"\bprivate(ly)? held\b", "privately_held"),
    (r"\bfounded (in )?(19|20)\d{2}\b", "founded_year"),
)

TIER1 = (
    (r"\b(30|35|40)\+?\s*years\b", "long_tenure"),
    (r"\bmore than (30|35|40) years\b", "long_tenure"),
    (r"\bfounder since\b", "founder_longevity"),
    (r"\bsecond[- ]generation\b", "second_generation"),
    (r"\bthird[- ]generation\b", "third_generation"),
    (r"\bfourth[- ]generation\b", "fourth_generation"),
    (r"\bmulti[- ]generation(al)?\b", "multi_generation"),
    (r"\bnext generation\b", "next_generation_weak"),
    (r"\bleadership transition\b", "leadership_transition"),
)

TIER2 = (
    (r"\bstepping back\b", "founder_step_back"),
    (r"\bnew president\b", "new_president"),
    (r"\bsuccession planning\b", "succession_planning"),
    (r"\btransitioning leadership\b", "transitioning_leadership"),
    (r"\btransitioned from (president|ceo|owner)\b", "leadership_handoff"),
    (r"\bassumed leadership\b", "assumed_leadership"),
    (r"\btook over (as )?(president|ceo|owner|leadership)\b", "took_over_leadership"),
    (r"\bnext chapter\b", "next_chapter"),
    (r"\bstrategic (partner|options)\b", "strategic_options"),
    (r"\bvp operations\b.*\b(daughter|son|family)\b", "family_vp"),
    (r"\b(daughter|son)\b.*\b(vp|president|ceo|operations|leadership|assumed)\b", "family_executive"),
    (r"\b(daughter|son)\b.*\b(owns|owner|operates)\b", "family_owner_successor"),
    (r"\badvisory (committee|board|role|chairman|chair)\b", "moved_to_advisory"),
)

TIER3 = (
    (r"\bretir(e|ed|ing|ement)\b", "retirement"),
    (r"\balready retired\b", "already_retired"),
    (r"\bsuccession (plan|process) underway\b", "succession_underway"),
    (r"\bownership transition\b", "ownership_transition"),
    (r"\bfamily succession\b", "family_succession"),
    (r"\blooking for (a )?strategic partner\b", "seeking_partner"),
)

TIER4 = (
    (r"\bbusiness for sale\b", "for_sale"),
    (r"\bseeking (a )?buyer\b", "seeking_buyer"),
    (r"\bfor sale by owner\b", "fsbo"),
    (r"\bacquisition (opportunity|target)\b", "acquisition_listing"),
)

COUNTER = (
    (r"\braised (\$|usd|cad)?\s*\d", "fundraising"),
    (r"\bexpanding (aggressively|rapidly)\b", "aggressive_expansion"),
    (r"\bopening (new )?locations?\b", "new_locations"),
    (r"\bacquired\b", "recent_acquisition"),
    (r"\byoung founder\b", "young_founder"),
    (r"\bhired (a )?new ceo\b", "new_ceo_external"),
)

TIER_STRENGTH = {0: 0.15, 1: 0.45, 2: 0.7, 3: 0.88, 4: 0.95}


def _match_tier(text: str, patterns: tuple, tier: int) -> list[dict]:
    out = []
    lower = text.lower()
    for pat, stype in patterns:
        if re.search(pat, lower, re.I):
            out.append({
                "type": stype,
                "tier": tier,
                "strength": TIER_STRENGTH[tier],
                "evidence": _snippet(lower, pat),
                "source": "text",
                "confidence": 0.7 + 0.05 * tier,
            })
    return out


def _snippet(text: str, pat: str, window: int = 80) -> str:
    m = re.search(pat, text, re.I)
    if not m:
        return ""
    start = max(0, m.start() - 40)
    end = min(len(text), m.end() + 40)
    return text[start:end].strip()


def classify_succession_signals(text: str, *, founded_year: int | None = None) -> dict:
    """
    Tiered succession/exit signals. Tier 0 alone must NOT qualify as exit intent.
    """
    from .keira_evidence import aggregate_evidence_confidence, weight_signals

    text = text or ""
    signals: list[dict] = []
    for tier, pats in ((0, TIER0), (1, TIER1), (2, TIER2), (3, TIER3), (4, TIER4)):
        signals.extend(_match_tier(text, pats, tier))

    if founded_year and founded_year <= 2000:
        signals.append({
            "type": "founder_longevity_proxy",
            "tier": 1,
            "strength": 0.5,
            "evidence": f"Founded {founded_year} — long operating history",
            "source": "founded_year",
            "confidence": 0.65,
        })

    counters = []
    for pat, stype in COUNTER:
        if re.search(pat, text, re.I):
            counters.append({
                "type": stype,
                "evidence": _snippet(text.lower(), pat),
                "strength": 0.6,
            })

    # Dedupe by type keeping highest tier
    by_type: dict[str, dict] = {}
    for s in signals:
        prev = by_type.get(s["type"])
        if not prev or s["tier"] > prev["tier"]:
            by_type[s["type"]] = s
    signals = weight_signals(list(by_type.values()))

    max_tier = max((s["tier"] for s in signals), default=0)
    # Probability: need tier>=1 meaningfully; tier0 alone → low
    # Weight by effective_strength (source reliability × decay)
    if max_tier == 0:
        prob = 0.15 if signals else 0.05
    else:
        base = {1: 0.4, 2: 0.62, 3: 0.78, 4: 0.9}.get(max_tier, 0.3)
        eff = [float(s.get("effective_strength") or s.get("strength") or 0) for s in signals if s.get("tier", 0) >= 1]
        bonus = min(0.15, 0.05 * (len(eff) - 1)) if eff else 0
        strength_adj = (sum(eff) / len(eff) - 0.5) * 0.2 if eff else 0
        penalty = min(0.25, 0.08 * len(counters))
        prob = max(0.05, min(0.95, base + bonus + strength_adj - penalty))

    recommendation = "reject"
    if prob >= 0.7 and max_tier >= 2:
        recommendation = "outreach_candidate"
    elif prob >= 0.45 and max_tier >= 1:
        recommendation = "research_further"
    elif max_tier >= 1:
        recommendation = "watchlist"
    else:
        recommendation = "context_only"

    return {
        "succession_probability": round(prob, 2),
        "max_tier": max_tier,
        "time_horizon": "6-24_months" if max_tier >= 1 else "unknown",
        "signals": signals,
        "counter_signals": counters,
        "recommendation": recommendation,
        "direct_intent": max_tier >= 4,
        "evidence_confidence": aggregate_evidence_confidence(signals),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def succession_score_0_to_25(result: dict) -> int:
    """Map succession analysis to 0–25 points for dimensional scoring."""
    prob = float(result.get("succession_probability") or 0)
    tier = int(result.get("max_tier") or 0)
    if tier == 0:
        return min(5, int(prob * 10))
    return min(25, int(round(prob * 25)))
