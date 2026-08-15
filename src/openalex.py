"""OpenAlex — free author / affiliation backstop (no API key)."""

from __future__ import annotations

import logging
import re

import httpx

from .email_patterns import normalize_domain

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
UA = "JayAgents/1.0 (mailto:research@jayagents.dev)"


def search_author_emails(
    company: str,
    *,
    domain: str | None = None,
    max_emails: int = 15,
) -> list[dict]:
    """
    Search OpenAlex works mentioning company affiliation; extract author emails when present.
    OpenAlex often omits emails — treat as thin backstop after Europe PMC.
    """
    company = (company or "").strip()
    if not company:
        return []

    prefer = normalize_domain(domain)
    found: list[dict] = []
    seen: set[str] = set()

    try:
        r = httpx.get(
            "https://api.openalex.org/works",
            params={
                "filter": f"raw_affiliation_strings.search:{company}",
                "per-page": 25,
                "select": "id,display_name,authorships",
            },
            headers={"User-Agent": UA},
            timeout=25.0,
        )
        if r.status_code != 200:
            logger.debug("OpenAlex %s for %s", r.status_code, company)
            return []
        results = (r.json() or {}).get("results") or []
    except Exception as e:
        logger.warning("OpenAlex failed for %s: %s", company, e)
        return []

    for work in results:
        for auth in work.get("authorships") or []:
            author = auth.get("author") or {}
            # Rare: raw affiliation may embed email
            for raw in auth.get("raw_affiliation_strings") or []:
                for email in EMAIL_RE.findall(raw):
                    email = email.lower().rstrip(".")
                    host = normalize_domain(email.split("@")[-1])
                    if prefer and host != prefer and not (host or "").endswith("." + prefer):
                        continue
                    if email in seen:
                        continue
                    seen.add(email)
                    name = (author.get("display_name") or "").strip()
                    parts = name.split()
                    found.append({
                        "email": email,
                        "first_name": parts[0] if len(parts) >= 2 else None,
                        "last_name": parts[-1] if len(parts) >= 2 else None,
                        "source": "openalex",
                        "title": (work.get("display_name") or "")[:200] or None,
                    })
                    if len(found) >= max_emails:
                        return found
    return found
