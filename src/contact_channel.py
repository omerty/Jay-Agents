"""Contact channel helpers — email vs LinkedIn vs incomplete."""

from __future__ import annotations


def _has_email(lead: dict) -> bool:
    email = (lead.get("email") or "").strip()
    return bool(email and "@" in email)


def _has_linkedin(lead: dict) -> bool:
    return bool((lead.get("linkedin_url") or "").strip())


def _has_contact_name(lead: dict) -> bool:
    return bool((lead.get("contact_name") or "").strip())


def contact_channel_info(lead: dict) -> dict:
    """Derive outreach channel metadata for API + dashboard."""
    if _has_email(lead):
        return {
            "channel": "email",
            "label": "Email available",
            "message": None,
            "can_gmail": True,
        }

    if _has_linkedin(lead) and _has_contact_name(lead):
        return {
            "channel": "linkedin",
            "label": "Email not found",
            "message": "Email not found — LinkedIn is the best channel for outreach.",
            "can_gmail": False,
        }

    if _has_linkedin(lead):
        return {
            "channel": "linkedin",
            "label": "Email not found",
            "message": "Email not found — try reaching out via LinkedIn.",
            "can_gmail": False,
        }

    if _has_contact_name(lead):
        return {
            "channel": "incomplete",
            "label": "No contact method",
            "message": "No email or LinkedIn on file — run contact search or add details manually.",
            "can_gmail": False,
        }

    return {
        "channel": "company_only",
        "label": "Company only",
        "message": "No named contact yet — run contact search to find an owner or decision-maker.",
        "can_gmail": False,
    }


def enrich_lead(lead: dict) -> dict:
    """Attach contact_channel fields to a lead dict for API responses."""
    from .hunter_api import hunter_available

    info = contact_channel_info(lead)
    out = dict(lead)
    out["contact_channel"] = info["channel"]
    out["contact_label"] = info["label"]
    out["contact_message"] = info["message"]
    out["can_gmail"] = info["can_gmail"]
    out["can_hunter_research"] = (
        hunter_available()
        and not _has_email(lead)
        and _has_contact_name(lead)
    )
    return out
