"""Keira company intelligence + dimensional M&A qualification + thesis."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .keira_geo import geo_score
from .keira_ownership import classify_ownership
from .keira_signals import classify_succession_signals, succession_score_0_to_25
from .owner_resolution import classify_owner_title, resolve_owner_from_lead

logger = logging.getLogger(__name__)


def _coerce_employees(val: Any) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    m = re.search(r"(\d[\d,]*)", str(val))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _founded_year(text: str) -> int | None:
    m = re.search(r"founded (?:in )?((?:19|20)\d{2})", text or "", re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(19[5-9]\d|20[0-1]\d)\b", text or "")
    return int(m.group(1)) if m else None


def size_score_0_to_20(employees: int | None) -> dict:
    if employees is None:
        return {"score": 8, "pass": True, "reason": "size unknown — soft pass"}
    if 20 <= employees <= 500:
        return {"score": 20, "pass": True, "reason": f"{employees} employees — sweet spot"}
    if 15 <= employees < 20:
        return {"score": 10, "pass": True, "reason": f"{employees} employees — small but possible"}
    if 500 < employees <= 1500:
        return {"score": 8, "pass": True, "reason": f"{employees} employees — upper band"}
    if employees < 15:
        return {"score": 0, "pass": False, "reason": f"{employees} employees — too small"}
    return {"score": 0, "pass": False, "reason": f"{employees} employees — too large for Keira"}


def business_quality_0_to_10(text: str, *, founded: int | None = None) -> dict:
    score = 4
    reasons = []
    lower = (text or "").lower()
    if founded and founded <= 2000:
        score += 3
        reasons.append(f"long operating history ({founded})")
    if any(w in lower for w in ("multi-location", "locations", "branches")):
        score += 2
        reasons.append("multi-location")
    if any(w in lower for w in ("b2b", "industrial", "manufacturer", "distributor", "wholesale")):
        score += 1
        reasons.append("B2B / industrial profile")
    return {"score": min(10, score), "reasons": reasons}


def build_company_intelligence(lead: dict, *, research_text: str | None = None) -> dict:
    """Assemble Company Intelligence object before expensive enrichment."""
    company = (lead.get("company") or "").strip()
    text = " ".join(filter(None, [
        research_text,
        lead.get("signal"),
        lead.get("industry"),
        lead.get("description"),
    ]))
    employees = _coerce_employees(lead.get("employee_count"))
    # Infer headcount from Actava-style prose when numeric field missing
    if employees is None:
        m = re.search(
            r"(?:employs|employing|staff of|workforce of|approximately|more than)\s+"
            r"(?:more than\s+)?(\d[\d,]*)\s*(?:\+?\s*)?(?:employees|electricians|staff|people)",
            text,
            re.I,
        )
        if m:
            employees = int(m.group(1).replace(",", ""))
        else:
            m = re.search(r"(\d[\d,]*)\+?\s*(?:employees|electricians|full-time staff)", text, re.I)
            if m:
                employees = int(m.group(1).replace(",", ""))

    founded = _founded_year(text)

    # Infer corridor city from prose when HQ fields empty
    city = lead.get("company_city") or lead.get("city")
    hq = lead.get("hq") or city
    if not city:
        from .keira_geo import STRONG_GEO, geo_score as _gs

        probe = _gs(extra_text=text)
        if probe.get("strong_city") and probe["strong_city"] != "eastern ontario":
            city = probe["strong_city"].title()
            hq = hq or city

    geo = geo_score(
        hq=hq,
        city=city,
        state=lead.get("company_state") or lead.get("state"),
        owner_location=lead.get("owner_location"),
        extra_text=text,
    )

    # Infer title from signal when Actava omitted it
    title = lead.get("contact_title") or lead.get("title")
    if not title and lead.get("contact_name"):
        cname = re.escape(lead["contact_name"])
        m = re.search(
            rf"(?:{cname}).{{0,80}}?\b(founder(?:\s*&|\s+and)?\s*president|owner(?:\s*/\s*operator)?|president|ceo)\b"
            rf"|\b(founder(?:\s*&|\s+and)?\s*president|owner(?:\s*/\s*operator)?|president|ceo)\b.{{0,80}}?(?:{cname})",
            text,
            re.I,
        )
        if m:
            title = next(g for g in m.groups() if g)
        elif re.search(rf"{cname}.{{0,120}}?\b(advisory|transitioned from president)\b", text, re.I):
            title = "President"

    lead_for_owner = {**lead, "contact_title": title, "company_city": city}
    owner = resolve_owner_from_lead(lead_for_owner)
    ownership = classify_ownership(company=company, text=text, title=title)
    succession = classify_succession_signals(text, founded_year=founded)
    size = size_score_0_to_20(employees)
    quality = business_quality_0_to_10(text, founded=founded)

    # Missing title is not a hard reject in company-first flow — defer to research.
    title_present = bool((owner.get("title") or "").strip())
    owner_reject = bool(owner.get("reject")) and title_present
    corp_reject = bool(ownership.get("reject"))

    return {
        "company": {
            "name": company,
            "domain": lead.get("company_domain") or lead.get("domain"),
            "hq": city or geo.get("strong_city"),
            "industry": lead.get("industry"),
            "founded": founded,
            "employees": employees,
            "revenue_estimate": lead.get("estimated_valuation") or lead.get("revenue"),
            "ownership_type": ownership["ownership_type"],
        },
        "ownership": {
            "owner_name": owner.get("person"),
            "title": owner.get("title"),
            "ownership_confidence": owner.get("ownership_confidence"),
            "founder": owner.get("is_founder"),
            "family_owned": ownership["ownership_type"] == "family_owned",
            "ownership_type": ownership["ownership_type"],
            "ownership_allowed": ownership.get("allowed"),
            "reject": owner_reject or corp_reject,
            "reason": (
                ownership.get("reason") if corp_reject
                else owner.get("reason") if owner_reject
                else ownership.get("reason") or owner.get("reason")
            ),
            "needs_owner_resolution": not title_present,
        },
        "geo": geo,
        "size": size,
        "business_quality": quality,
        "succession": succession,
        "evidence_text": text[:2000],
        "source": lead.get("source"),
    }


def qualify_keira_intelligence(intel: dict, *, config: dict | None = None) -> dict:
    """
    Dimensional scoring with hard gates.
    GEO 0–20, OWNERSHIP 0–20, SIZE 0–20, QUALITY 0–10, SUCCESSION 0–25, CONFIDENCE 0–5.
    """
    config = config or {}
    gates = (config.get("keira") or {}).get("gates") or {}
    min_geo = int(gates.get("min_geo", 15))
    min_owner_conf = float(gates.get("min_owner_confidence", 0.70))
    min_succession = float(gates.get("min_succession", 0.40))
    min_evidence = int(gates.get("min_evidence_signals", 1))

    geo = intel.get("geo") or {}
    ownership = intel.get("ownership") or {}
    size = intel.get("size") or {}
    quality = intel.get("business_quality") or {}
    succession = intel.get("succession") or {}

    reject_reasons: list[str] = []

    if geo.get("reject") or geo.get("score", 0) < min_geo:
        reject_reasons.append(f"geo fail (score={geo.get('score')})")
    if ownership.get("reject"):
        reject_reasons.append(f"ownership reject: {ownership.get('reason')}")
    needs_owner = bool(ownership.get("needs_owner_resolution"))
    owner_conf = float(ownership.get("ownership_confidence") or 0)
    if not needs_owner and owner_conf < min_owner_conf:
        reject_reasons.append(f"owner confidence {owner_conf} < {min_owner_conf}")
    if not size.get("pass", True):
        reject_reasons.append(size.get("reason") or "size fail")

    succ_prob = float(succession.get("succession_probability") or 0)
    max_tier = int(succession.get("max_tier") or 0)
    # Tier-0-only (family-owned / private) is not exit evidence — soft gate for outreach,
    # not a hard reject so company-first research can still proceed.
    weak_succession = max_tier == 0 and succ_prob < 0.25

    # Dimensional scores
    geo_pts = int(geo.get("score") or 0)
    owner_pts = int(round(float(ownership.get("ownership_confidence") or 0) * 20))
    size_pts = int(size.get("score") or 0)
    quality_pts = int(quality.get("score") or 0)
    succ_pts = succession_score_0_to_25(succession)
    # Research confidence: evidence-weighted (source reliability × decay), not raw count
    from .keira_budget import research_priority
    from .keira_evidence import aggregate_evidence_confidence

    n_sig = len([s for s in (succession.get("signals") or []) if s.get("tier", 0) >= 1])
    ev_conf = float(
        succession.get("evidence_confidence")
        or aggregate_evidence_confidence(succession.get("signals") or [])
    )
    conf_pts = min(5, max(1, int(round(ev_conf * 5))))
    if n_sig:
        conf_pts = min(5, conf_pts + min(2, n_sig // 2))

    if succ_prob < min_succession and max_tier < 2:
        # Soft: don't hard reject but cap succession points
        succ_pts = min(succ_pts, 8)

    total = geo_pts + owner_pts + size_pts + quality_pts + succ_pts + conf_pts
    total = max(0, min(100, total))

    rejected = bool(reject_reasons)
    # Why now / why not — prefer Claude analyst why_now when present
    analyst = intel.get("analyst") or {}
    why_now = analyst.get("why_now") or _build_why_now(intel)
    why_not = list(reject_reasons)
    for c in succession.get("counter_signals") or []:
        why_not.append(f"counter: {c.get('type')} — {c.get('evidence', '')[:80]}")
    for w in analyst.get("why_not") or []:
        if w and w not in why_not:
            why_not.append(str(w)[:160])
    if max_tier == 0:
        why_not.append("No tier-1+ succession signal (family-owned alone is not exit intent)")

    if not why_now and not rejected:
        why_not.append("Could not articulate a credible why-now")
        outreach_ok = False
    else:
        outreach_ok = (
            not rejected
            and not needs_owner
            and not weak_succession
            and max_tier >= 2
            and succ_prob >= min_succession
            and bool(why_now)
        )

    if needs_owner and not rejected:
        why_not.append("Owner not yet resolved — enrich before outreach")

    icp_fit = min(100, geo_pts + owner_pts + size_pts + quality_pts + conf_pts)
    timing = int(round(succ_prob * 100))
    evidence_confidence = min(100, int(round(ev_conf * 100)))

    base = {
        "dimensions": {
            "geography": geo_pts,
            "ownership": owner_pts,
            "size": size_pts,
            "business_quality": quality_pts,
            "succession": succ_pts,
            "research_confidence": conf_pts,
        },
        "score": total,
        "icp_fit": icp_fit,
        "succession_signal": timing,
        "timing": timing,
        "evidence_confidence": evidence_confidence,
        "tier": "hot" if total >= 75 and outreach_ok else "warm" if total >= 50 and not rejected else "cold",
        "status": (
            "reject" if rejected else ("qualify" if outreach_ok else "research_required")
        ),
        "rejected": rejected,
        "reject_reasons": reject_reasons,
        "outreach_ok": outreach_ok,
        "why_now": why_now,
        "why_not": why_not,
        "recommendation": (
            "Skip — failed hard gates"
            if rejected
            else ("Draft confidential outreach" if outreach_ok else "Research further before outreach")
        ),
    }
    priority = research_priority(base, intel)
    base.update(priority)
    return base


def _build_why_now(intel: dict) -> str | None:
    company = (intel.get("company") or {}).get("name") or "the company"
    owner = (intel.get("ownership") or {}).get("owner_name") or "the owner"
    succ = intel.get("succession") or {}
    signals = [s for s in (succ.get("signals") or []) if s.get("tier", 0) >= 1]
    if not signals:
        return None
    bits = []
    founded = (intel.get("company") or {}).get("founded")
    if founded:
        bits.append(f"operating since {founded}")
    for s in sorted(signals, key=lambda x: -x.get("tier", 0))[:3]:
        ev = (s.get("evidence") or s.get("type") or "").strip()
        if ev:
            bits.append(ev[:120])
    if not bits:
        return None
    return (
        f"{owner} at {company}: "
        + "; ".join(bits)
        + ". This suggests a possible 6–24 month ownership/leadership transition window."
    )


def build_mna_thesis(intel: dict, qualification: dict) -> dict:
    """Analyst-style M&A thesis for outreach consumption."""
    co = intel.get("company") or {}
    own = intel.get("ownership") or {}
    succ = intel.get("succession") or {}
    return {
        "company": co.get("name"),
        "potential_owner": own.get("owner_name"),
        "title": own.get("title"),
        "thesis": (
            f"{'Founder-led' if own.get('founder') else 'Owner-operated'} "
            f"{co.get('industry') or 'business'} in {co.get('hq') or 'Eastern Ontario'}, "
            f"~{co.get('employees') or '?'} employees"
            f"{f', founded {co.get('founded')}' if co.get('founded') else ''}."
        ),
        "transaction_context": (
            "Succession / partial liquidity / strategic options"
            if int(succ.get("max_tier") or 0) >= 2
            else "Exploratory confidential conversation"
        ),
        "confidence": qualification.get("evidence_confidence"),
        "why_now": qualification.get("why_now"),
        "key_uncertainty": (
            "; ".join((qualification.get("why_not") or [])[:2])
            or "No public confirmation owner intends to sell"
        ),
        "direct_intent": bool(succ.get("direct_intent")),
        "succession_probability": succ.get("succession_probability"),
    }


def intelligence_to_qualification_result(intel: dict, q: dict) -> dict:
    """Shape for pipeline.process_lead / upsert compatibility."""
    reasons = []
    for k, v in (q.get("dimensions") or {}).items():
        reasons.append(f"{k}={v}")
    if q.get("why_now"):
        reasons.append(f"Why now: {q['why_now'][:160]}")
    for r in (q.get("reject_reasons") or [])[:3]:
        reasons.append(f"Gate: {r}")
    return {
        "score": q["score"],
        "tier": q["tier"],
        "reasons": reasons,
        "talking_points": [q["why_now"]] if q.get("why_now") else [],
        "recommendation": q.get("recommendation") or "",
        "confidence": q.get("evidence_confidence"),
        "estimated_company_size": (intel.get("company") or {}).get("revenue_estimate"),
        "ownership_confidence": (intel.get("ownership") or {}).get("ownership_confidence"),
        "succession_signals": [
            s.get("type") for s in ((intel.get("succession") or {}).get("signals") or [])
        ],
        "mode": "keira_intelligence",
        "keira": q,
        "mna_thesis": build_mna_thesis(intel, q),
    }
