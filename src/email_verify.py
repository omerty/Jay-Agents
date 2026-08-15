"""Email verification — Hunter verifier first, MX fallback."""

from __future__ import annotations

import logging
import os
import socket

import httpx

from .hunter_api import BASE_URL, hunter_available

logger = logging.getLogger(__name__)


def verify_enabled() -> bool:
    return os.getenv("EMAIL_VERIFY_BEFORE_DRAFT", "true").lower() not in ("0", "false", "no")


def mx_records_exist(domain: str) -> bool:
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain:
        return False
    try:
        import dns.resolver  # type: ignore

        answers = dns.resolver.resolve(domain, "MX")
        return bool(list(answers))
    except Exception:
        pass
    # Fallback: any A/AAAA on domain is weak signal only
    try:
        socket.getaddrinfo(domain, 25)
        return True
    except OSError:
        return False


def verify_email_hunter(email: str) -> dict | None:
    """Hunter Email Verifier. Returns {status, score, result} or None."""
    api_key = os.getenv("HUNTER_API_KEY", "")
    email = (email or "").strip().lower()
    if not api_key or not email or "@" not in email:
        return None
    try:
        r = httpx.get(
            f"{BASE_URL}/email-verifier",
            params={"email": email, "api_key": api_key},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        logger.warning("Hunter verify failed: %s", e)
        return None
    if r.status_code in (401, 429):
        logger.warning("Hunter verify %s", r.status_code)
        return None
    if r.status_code != 200:
        return None
    data = (r.json() or {}).get("data") or {}
    return {
        "status": (data.get("status") or "").lower(),
        "result": (data.get("result") or data.get("status") or "").lower(),
        "score": data.get("score"),
        "email": data.get("email") or email,
    }


def verify_email(email: str) -> dict:
    """
    Verify an email address.
    Returns {ok, verified, status, method, detail}.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "verified": False, "status": "invalid", "method": None, "detail": "bad format"}

    domain = email.split("@", 1)[-1]
    if hunter_available():
        hv = verify_email_hunter(email)
        if hv:
            status = hv.get("status") or hv.get("result") or ""
            # Hunter: valid / invalid / accept_all / webmail / disposable / unknown
            if status in ("valid",):
                return {"ok": True, "verified": True, "status": status, "method": "hunter", "detail": status}
            if status in ("accept_all", "webmail"):
                # Deliverable-ish but not person-verified
                return {"ok": True, "verified": False, "status": status, "method": "hunter", "detail": status}
            if status in ("invalid", "disposable"):
                return {"ok": False, "verified": False, "status": status, "method": "hunter", "detail": status}
            # unknown — fall through to MX
            logger.debug("Hunter unknown for %s — MX fallback", email)

    if mx_records_exist(domain):
        return {"ok": True, "verified": False, "status": "mx_ok", "method": "mx", "detail": "MX present"}
    return {"ok": False, "verified": False, "status": "no_mx", "method": "mx", "detail": "no MX"}


def apply_verification_to_lead(lead: dict, result: dict) -> dict:
    """Mutate lead fields from verification result. Returns patch dict for set_lead_fields."""
    patch: dict = {}
    if not result.get("ok"):
        patch["email_verified"] = 0
        if result.get("status") in ("invalid", "disposable", "no_mx"):
            # Demote — keep email but mark inferred/unverified
            patch["email_inferred"] = 1
            patch["contact_tier"] = "B"
        return patch

    if result.get("verified"):
        patch["email_verified"] = 1
        patch["email_inferred"] = 0
        patch["email_source"] = lead.get("email_source") or "hunter_verify"
        patch["contact_tier"] = "A"
    else:
        # accept_all / mx_ok — keep as B if inferred, else soft A unknown
        if lead.get("email_inferred") or (lead.get("email_source") or "").startswith("pattern"):
            patch["email_verified"] = 0
            patch["contact_tier"] = "B"
        else:
            patch["email_verified"] = 0
            patch["contact_tier"] = lead.get("contact_tier") or "B"
    return patch


def verify_lead_email(lead: dict) -> dict:
    """Verify lead email and persist tier/verified flags. Returns verification result."""
    from .contact_tier import classify_contact_tier
    from .db import set_lead_fields

    email = (lead.get("email") or "").strip()
    if not email:
        return {"ok": False, "skipped": True, "detail": "no email"}
    if lead.get("email_verified") and (lead.get("contact_tier") or "").upper() == "A":
        return {"ok": True, "verified": True, "skipped": True, "detail": "already verified"}

    result = verify_email(email)
    patch = apply_verification_to_lead(lead, result)
    merged = {**lead, **patch}
    patch["contact_tier"] = classify_contact_tier(merged)
    if lead.get("id"):
        set_lead_fields(lead["id"], **patch)
    result["patch"] = patch
    result["contact_tier"] = patch["contact_tier"]
    return result
