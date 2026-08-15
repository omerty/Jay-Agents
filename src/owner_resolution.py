"""Keira owner resolution — founder/owner vs corp exec / broker."""

from __future__ import annotations

import re

OWNER_EXCELLENT = (
    r"\bfounder\b.*\bpresident\b",
    r"\bpresident\b.*\bfounder\b",
    r"\bowner\b.*\bpresident\b",
    r"\bpresident\b.*\bowner\b",
    r"\bfounder\s*(&|and)\s*ceo\b",
    r"\bceo\s*(&|and)\s*founder\b",
    r"\bowner\s*/\s*operator\b",
    r"\bfounding\s+owner\b",
)

OWNER_GOOD = (
    r"\bowner\b",
    r"\bfounder\b",
    r"\bco-?founder\b",
    r"\bpresident\b",
    r"\bchief executive\b",
    r"\bceo\b",
    r"\bmanaging partner\b",
    r"\bprincipal\b",
)

OWNER_REJECT = (
    r"\bvp\b",
    r"\bvice president\b",
    r"\bdirector of\b",
    r"\bregional (president|vp|director|manager)\b",
    r"\bcountry manager\b",
    r"\bgeneral manager,? canada\b",
    r"\bmanaging director,? canada\b",
    r"\bsales\b",
    r"\bmarketing\b",
    r"\bhr\b",
    r"\brecruiter\b",
    r"\bbroker\b",
    r"\badvisor\b",
    r"\bconsultant\b",
)

CORP_EXEC_HINTS = (
    r"\binc\.?\b.*\bcanada\b",
    r"\bcanada\b.*\bdivision\b",
    r"\bsubsidiary\b",
    r"\bnational\b",
)


def classify_owner_title(title: str | None, *, company: str | None = None) -> dict:
    """
    Classify whether a title is a credible private-company owner/founder.
    Returns ownership_confidence 0–1 and is_owner / is_founder flags.
    """
    title = (title or "").strip()
    lower = title.lower()
    company_l = (company or "").lower()

    if not title:
        return {
            "title": title,
            "is_owner": False,
            "is_founder": False,
            "ownership_confidence": 0.0,
            "tier": "unknown",
            "reject": True,
            "reason": "no title",
        }

    for pat in OWNER_REJECT:
        if re.search(pat, lower):
            return {
                "title": title,
                "is_owner": False,
                "is_founder": False,
                "ownership_confidence": 0.1,
                "tier": "reject",
                "reject": True,
                "reason": f"non-owner title pattern: {pat}",
            }

    # Regional / subsidiary exec
    if re.search(r"\b(regional|district|area|zone)\b", lower):
        return {
            "title": title,
            "is_owner": False,
            "is_founder": False,
            "ownership_confidence": 0.15,
            "tier": "reject",
            "reject": True,
            "reason": "regional executive — not equity owner",
        }

    # "President, Canada" / "VP Ontario" at a national / multinational brand
    if re.search(r"\b(canada|ontario|eastern canada|atlantic)\b", lower) and re.search(
        r"\b(president|vp|vice president|general manager|managing director)\b", lower
    ):
        return {
            "title": title,
            "is_owner": False,
            "is_founder": False,
            "ownership_confidence": 0.2,
            "tier": "reject",
            "reject": True,
            "reason": "geographic corporate executive — not equity owner",
        }

    # National / multinational brands — president/CEO without owner/founder is corp exec
    national_brands = (
        r"\b(coca-?cola|pepsico|pepsi|walmart|costco|amazon|microsoft|google|"
        r"ibm|ge\b|general electric|siemens|honeywell|3m|unilever|nestl[eé]|"
        r"procter\s*&\s*gamble|p&g|toyota|honda|ford|gm|general motors|"
        r"home\s*depot|lowes|lowe's|best\s*buy|staples|fedex|ups|"
        r"rbc|td\b|scotiabank|bmo|cibc|bell\b|rogers|telus|"
        r"shopify|shoppers|loblaw|sobeys|metro\s+inc)\b"
    )
    if company_l and re.search(national_brands, company_l):
        if not re.search(r"\b(owner|founder|co-?founder)\b", lower):
            return {
                "title": title,
                "is_owner": False,
                "is_founder": False,
                "ownership_confidence": 0.15,
                "tier": "reject",
                "reject": True,
                "reason": "corporate executive at national brand — not private owner",
            }

    # "X Canada Division" / "National President" / subsidiary-sounding companies
    if re.search(r"\b(division|subsidiary|national|canada\s+inc)\b", company_l):
        if re.search(r"\b(president|ceo|managing director|general manager)\b", lower) and not re.search(
            r"\b(owner|founder|co-?founder)\b", lower
        ):
            return {
                "title": title,
                "is_owner": False,
                "is_founder": False,
                "ownership_confidence": 0.2,
                "tier": "reject",
                "reject": True,
                "reason": "division/national corporate executive — not equity owner",
            }

    if re.search(r"\bnational\s+(president|vp|director|manager)\b", lower):
        return {
            "title": title,
            "is_owner": False,
            "is_founder": False,
            "ownership_confidence": 0.15,
            "tier": "reject",
            "reject": True,
            "reason": "national corporate executive — not equity owner",
        }

    is_founder = bool(re.search(r"\bfounder\b|\bco-?founder\b", lower))
    is_owner = bool(re.search(r"\bowner\b", lower)) or is_founder

    for pat in OWNER_EXCELLENT:
        if re.search(pat, lower):
            return {
                "title": title,
                "is_owner": True,
                "is_founder": is_founder or "founder" in lower,
                "ownership_confidence": 0.94,
                "tier": "excellent",
                "reject": False,
                "reason": "founder/owner + president/ceo",
            }

    if is_owner or is_founder:
        conf = 0.91 if is_founder else 0.88
        return {
            "title": title,
            "is_owner": True,
            "is_founder": is_founder,
            "ownership_confidence": conf,
            "tier": "excellent",
            "reject": False,
            "reason": "explicit owner/founder",
        }

    if re.search(r"\bpresident\b|\bceo\b|\bchief executive\b", lower):
        # President of "X Canada" is often a corp exec
        if re.search(r"\bcanada\b", company_l) and re.search(r"\b(inc|corp|ltd|limited|plc)\b", company_l):
            # ambiguous
            return {
                "title": title,
                "is_owner": False,
                "is_founder": False,
                "ownership_confidence": 0.55,
                "tier": "weak",
                "reject": False,
                "reason": "president/ceo — ownership unclear (possible subsidiary)",
            }
        return {
            "title": title,
            "is_owner": True,  # private co president often is owner-operator
            "is_founder": False,
            "ownership_confidence": 0.78,
            "tier": "good",
            "reject": False,
            "reason": "president/ceo of private company (assumed operator)",
        }

    for pat in OWNER_GOOD:
        if re.search(pat, lower):
            return {
                "title": title,
                "is_owner": True,
                "is_founder": is_founder,
                "ownership_confidence": 0.72,
                "tier": "good",
                "reject": False,
                "reason": "owner-like title",
            }

    return {
        "title": title,
        "is_owner": False,
        "is_founder": False,
        "ownership_confidence": 0.25,
        "tier": "weak",
        "reject": True,
        "reason": "title not owner/founder/president/ceo",
    }


def is_owner_like_title(title: str | None, *, company: str | None = None) -> bool:
    """True for equity-owner titles (Owner/Founder/President/CEO), not VP/regional execs."""
    classified = classify_owner_title(title, company=company)
    return bool(classified.get("is_owner")) and not classified.get("reject")


def resolve_owner_from_lead(lead: dict) -> dict:
    """Build owner resolution object from a lead/search row."""
    name = (lead.get("contact_name") or "").strip() or None
    title = lead.get("contact_title") or lead.get("title")
    company = lead.get("company")
    classified = classify_owner_title(title, company=company)
    return {
        "person": name,
        "title": classified["title"],
        "is_owner": classified["is_owner"],
        "is_founder": classified["is_founder"],
        "ownership_confidence": classified["ownership_confidence"],
        "tier": classified["tier"],
        "reject": classified["reject"],
        "reason": classified["reason"],
        "evidence": [
            {
                "field": "title",
                "value": title,
                "confidence": classified["ownership_confidence"],
                "source": lead.get("source") or "lead",
            }
        ] if title else [],
    }
