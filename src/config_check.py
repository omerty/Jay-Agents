"""Startup configuration validation — fail fast with actionable messages."""

import os

from .contacts import contacts_status
from .gmail_api import gmail_status
from .llm import check_llm
from .microsoft_mail_api import microsoft_status


def validate_config() -> dict:
    """Check required services before a daily run or dashboard startup.

    Returns {ok, issues, warnings, llm, contacts, gmail, microsoft}.
    *issues* block automation; *warnings* are informational (e.g. mail optional).
    """
    issues: list[str] = []
    warnings: list[str] = []

    llm = check_llm()
    if not llm["ok"]:
        issues.append(f"LLM unavailable ({llm.get('provider') or 'none'}): {llm['detail']}")

    contacts = contacts_status()
    if not contacts["configured"]:
        key = "APOLLO_API_KEY" if contacts["provider"] == "apollo" else "PDL_API_KEY"
        issues.append(
            f"Contact provider '{contacts['provider']}' has no API key — set {key} in .env"
        )

    provider = (os.getenv("CONTACTS_PROVIDER") or "").strip().lower()
    if provider == "apollo" and not os.getenv("APOLLO_API_KEY"):
        issues.append("CONTACTS_PROVIDER=apollo but APOLLO_API_KEY is missing")
    if provider == "pdl" and not os.getenv("PDL_API_KEY"):
        issues.append("CONTACTS_PROVIDER=pdl but PDL_API_KEY is missing")

    gmail = gmail_status()
    microsoft = microsoft_status()
    if not gmail["connected"] and not microsoft["connected"]:
        warnings.append(
            "No mailbox connected — drafts/replies disabled "
            f"(Gmail: {gmail.get('detail')}; Microsoft: {microsoft.get('detail')})"
        )
    elif not gmail["connected"]:
        warnings.append(f"Gmail not connected ({gmail.get('detail')})")
    elif not microsoft["connected"]:
        warnings.append(f"Microsoft Email not connected ({microsoft.get('detail')})")

    delay = float(os.getenv("LLM_CALL_DELAY", "0"))
    if llm.get("ok") and llm.get("provider") == "groq" and delay < 2.0:
        warnings.append(
            "Groq free tier: set LLM_CALL_DELAY=2.5 in .env to avoid rate limits during daily runs"
        )

    from .auth import auth_enabled

    if auth_enabled():
        if not (os.getenv("AUTH_SESSION_SECRET") or os.getenv("AUTH_ADMIN_SECRET")):
            warnings.append("AUTH_ENABLED but AUTH_SESSION_SECRET is not set — auth will fail on signup/login")
        if not os.getenv("AUTH_ADMIN_SECRET"):
            warnings.append("AUTH_ADMIN_SECRET is not set — you cannot create user invites")

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "llm": llm,
        "contacts": contacts,
        "gmail": gmail,
        "microsoft": microsoft,
    }
