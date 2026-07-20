"""Startup configuration validation — fail fast with actionable messages."""

import os

from .contacts import contacts_status
from .gmail_api import gmail_status
from .llm import check_llm


def validate_config() -> dict:
    """Check required services before a daily run or dashboard startup.

    Returns {ok, issues, warnings, llm, contacts, gmail}.
    *issues* block automation; *warnings* are informational (e.g. Gmail optional).
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
    if not gmail["connected"]:
        warnings.append(f"Gmail not connected — drafts/replies disabled ({gmail.get('detail')})")

    delay = float(os.getenv("LLM_CALL_DELAY", "0"))
    if llm["ok"] and llm.get("provider") == "groq" and delay < 2.0:
        warnings.append(
            "Groq free tier: set LLM_CALL_DELAY=2.5 in .env to avoid rate limits during daily runs"
        )

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "llm": llm,
        "contacts": contacts,
        "gmail": gmail,
    }
