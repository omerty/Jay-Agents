"""Contact confidence tiers A/B/C/D for outreach routing."""

from __future__ import annotations

import os


def classify_contact_tier(lead: dict) -> str:
    """
    A — verified email (Hunter verified or published verbatim in source)
    B — pattern-inferred email
    C — named contact, LinkedIn, no email
    D — company only, no person
    """
    email = (lead.get("email") or "").strip()
    name = (lead.get("contact_name") or "").strip()
    linkedin = (lead.get("linkedin_url") or "").strip()

    if not name:
        return "D"

    if email and "@" in email:
        # Only A when explicitly verified (or Hunter finder with verified flag)
        if lead.get("email_verified") in (1, True, "1") and lead.get("email_source") in (
            "hunter_finder", "hunter_domain", "hunter_verify", "published",
        ):
            return "A"
        if lead.get("email_verified") in (1, True, "1") and not lead.get("email_inferred"):
            return "A"
        if lead.get("email_inferred") or lead.get("email_source") in (
            "pattern", "hunter_pattern", "privacy_dpo", "mx", "accept_all",
        ):
            return "B"
        # Unknown provenance — treat as B until verified
        return "B"

    if linkedin:
        return "C"

    return "D"


def tier_min_for_draft() -> str:
    """Minimum tier to create an email draft (A or B by default)."""
    return (os.getenv("EMAIL_TIER_MIN_FOR_DRAFT") or "B").strip().upper()


def tier_allows_email_draft(tier: str) -> bool:
    order = {"A": 4, "B": 3, "C": 2, "D": 1}
    min_t = tier_min_for_draft()
    return order.get(tier, 0) >= order.get(min_t, 3)


def tier_label(tier: str) -> str:
    return {
        "A": "Verified email",
        "B": "Inferred email — verify before send",
        "C": "LinkedIn only",
        "D": "Company only",
    }.get(tier, tier)
