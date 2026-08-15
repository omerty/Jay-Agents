"""Reply classification and referral capture."""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta

from .db import add_suppression, record_outcome, upsert_lead, build_prospect_string, set_lead_fields

logger = logging.getLogger(__name__)

REPLY_CLASSES = ("positive", "referral", "not_now", "wrong_person", "negative", "ooo", "auto", "bounce")

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def classify_reply_text(body: str, *, use_llm: bool = True) -> dict:
    """Classify reply: positive/referral/not_now/wrong_person/negative/ooo/auto/bounce."""
    text = (body or "").strip()
    lower = text.lower()

    # Bounce / delivery failure
    if any(p in lower for p in (
        "delivery status notification", "undeliverable", "mail delivery failed",
        "mailbox unavailable", "user unknown", "does not exist", "550 ", "551 ",
        "address rejected", "permanent failure",
    )):
        return {"class": "bounce", "suppress": True, "suppress_email_only": True, "notes": "Bounce detected"}

    # Fast heuristics first
    if any(p in lower for p in ("out of office", "out-of-office", "automatic reply", "auto-reply", "away from")):
        return {"class": "ooo", "revive_at": _parse_return_date(lower), "notes": "Auto-detected OOO"}
    if any(p in lower for p in ("unsubscribe", "remove me", "stop emailing", "do not contact", "not interested")):
        return {"class": "negative", "suppress": True, "suppress_email_only": True, "notes": "Opt-out language"}
    if any(p in lower for p in ("wrong person", "not the right", "don't handle", "try reaching", "speak with")):
        return {"class": "wrong_person", "notes": "Wrong person signal"}

    if not use_llm or len(text) < 20:
        if any(p in lower for p in ("yes", "interested", "let's talk", "schedule", "call")):
            return {"class": "positive", "notes": "Keyword match"}
        if any(p in lower for p in ("later", "next quarter", "not now", "circle back", "reach out in")):
            return {"class": "not_now", "revive_at": _parse_revive_hint(lower), "notes": "Timing deferral"}
        return {"class": "auto", "notes": "Unclassified" if text else "Empty reply body"}

    from .llm import chat_json
    from .llm_optimize import task_defaults

    defaults = task_defaults("critique")
    try:
        result = chat_json(
            f"""Classify this sales email reply. Body:
{text[:2000]}

JSON only:
{{"class": "positive|referral|not_now|wrong_person|negative|ooo|bounce|auto", "revive_at": "YYYY-MM-DD or null", "referral_name": "name or null", "referral_title": "title or null", "notes": "brief"}}""",
            system="You classify B2B sales replies. Output strict JSON.",
            max_tokens=256,
            temperature=0.1,
        )
        cls = (result.get("class") or "auto").lower()
        if cls not in REPLY_CLASSES:
            cls = "auto"
        out = {"class": cls, "notes": result.get("notes", "")}
        if result.get("revive_at"):
            out["revive_at"] = result["revive_at"]
        elif cls == "ooo":
            out["revive_at"] = _parse_return_date(lower)
        if result.get("referral_name"):
            out["referral_name"] = result["referral_name"]
            out["referral_title"] = result.get("referral_title")
        if cls in ("negative", "bounce"):
            out["suppress"] = True
            out["suppress_email_only"] = True
        return out
    except Exception as e:
        logger.warning("Reply classification failed: %s", e)
        return {"class": "auto", "notes": str(e)}


def _parse_return_date(text: str) -> str | None:
    """Parse simple 'returning on March 15' / 'back Monday' style dates."""
    today = date.today()
    m = re.search(
        r"(?:return(?:ing)?|back|until|through)\s+(?:on\s+)?([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?",
        text,
    )
    if m:
        mon = MONTHS.get(m.group(1).lower())
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if mon:
            try:
                dt = date(year, mon, day)
                if dt < today:
                    dt = date(year + 1, mon, day)
                return dt.isoformat()
            except ValueError:
                pass
    if "next week" in text:
        return (today + timedelta(days=7)).isoformat()
    if "next month" in text:
        return (today + timedelta(days=30)).isoformat()
    return (today + timedelta(days=7)).isoformat()  # default OOO revive


def _parse_revive_hint(text: str) -> str | None:
    today = date.today()
    if "q1" in text or "january" in text or "february" in text or "march" in text:
        year = today.year if today.month <= 3 else today.year + 1
        return date(year, 1, 15).isoformat()
    if "q2" in text or "april" in text or "may" in text or "june" in text:
        year = today.year if today.month <= 6 else today.year + 1
        return date(year, 4, 15).isoformat()
    if "q3" in text or "july" in text or "august" in text or "september" in text:
        year = today.year if today.month <= 9 else today.year + 1
        return date(year, 7, 15).isoformat()
    if "q4" in text or "october" in text or "november" in text or "december" in text:
        return date(today.year, 10, 15).isoformat()
    if "next quarter" in text:
        return (today + timedelta(days=90)).isoformat()
    if "next month" in text:
        return (today + timedelta(days=30)).isoformat()
    return (today + timedelta(days=60)).isoformat()


def process_reply_for_lead(lead_id: int, reply_body: str, *, agent: str = "woodway") -> dict:
    """Classify reply, record outcome, handle referral/suppression."""
    from .db import get_lead

    lead = get_lead(lead_id)
    if not lead:
        return {"ok": False, "error": "lead not found"}

    classification = classify_reply_text(reply_body)
    cls = classification["class"]

    set_lead_fields(
        lead_id,
        status="replied",
        reply_class=cls,
        revive_at=classification.get("revive_at"),
    )
    record_outcome(
        lead_id, "replied" if cls != "bounce" else "bounce",
        reply_class=cls,
        revive_at=classification.get("revive_at"),
        notes=classification.get("notes"),
    )

    # Feed query learning when we know discovery query context
    try:
        from .query_learning import record_query_outcome
        signal = lead.get("signal") or ""
        if signal and cls in ("positive", "referral"):
            record_query_outcome(agent, signal, positive=True)
        elif signal and cls in ("negative", "bounce", "wrong_person"):
            record_query_outcome(agent, signal, positive=False)
    except Exception:
        pass

    referral_lead_id = None
    if cls == "referral" or classification.get("referral_name"):
        referral_lead_id = _create_referral_lead(lead, classification, agent=agent)

    if classification.get("suppress") or cls in ("negative", "bounce"):
        email = (lead.get("email") or "").strip().lower()
        domain = (lead.get("company_domain") or "").strip().lower()
        if email:
            reason = f"{cls} reply: {lead.get('company')}"
            add_suppression(email, scope="email", reason=reason, agent=agent)
        # Domain suppress only when explicitly requested (default: email-only to avoid nuking @jnj.com)
        allow_domain = os.getenv("SUPPRESS_DOMAIN_ON_NEGATIVE", "false").lower() in ("1", "true", "yes")
        if allow_domain and cls == "negative" and domain and not classification.get("suppress_email_only"):
            add_suppression(domain, scope="domain", reason=f"domain opt-out: {lead.get('company')}", agent=agent, domain=domain)

    # Stop pending sequence steps on terminal replies
    if cls in ("positive", "referral", "negative", "bounce", "wrong_person"):
        try:
            from .sequences import halt_sequence
            halt_sequence(lead_id, reason=cls)
        except Exception:
            pass

    return {
        "ok": True,
        "class": cls,
        "referral_lead_id": referral_lead_id,
        "classification": classification,
    }


def _create_referral_lead(original: dict, classification: dict, *, agent: str) -> int | None:
    name = (classification.get("referral_name") or "").strip()
    if not name or len(name.split()) < 2:
        return None
    company = original.get("company", "")
    referrer = original.get("contact_name") or "your colleague"
    record = {
        "company": company,
        "contact_name": name,
        "contact_title": classification.get("referral_title"),
        "signal": f"Referral from {referrer} at {company}",
        "source": "referral",
        "status": "imported",
        "company_domain": original.get("company_domain"),
    }
    record["prospect"] = build_prospect_string(record)
    lead_id, _ = upsert_lead(record, agent=agent)
    record_outcome(lead_id, "referral_captured", notes=f"Referred by {referrer}")
    return lead_id
