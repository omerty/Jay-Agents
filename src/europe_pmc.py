"""Europe PMC — free author emails from academic affiliations (no API key)."""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Common academic / vendor noise domains — never treat as company pattern
SKIP_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "example.com", "nih.gov", "who.int", "cdc.gov",
})


def _affiliation_emails(result: dict, *, prefer_domain: str | None = None) -> list[dict]:
    from .email_patterns import _domains_related, normalize_domain

    out: list[dict] = []
    authors = ((result.get("authorList") or {}).get("author") or [])
    for author in authors:
        first = (author.get("firstName") or "").strip()
        last = (author.get("lastName") or "").strip()
        affs = ((author.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or [])
        for aff in affs:
            text = aff.get("affiliation") or ""
            for email in EMAIL_RE.findall(text):
                email = email.lower().rstrip(".")
                if email.endswith("."):
                    email = email[:-1]
                domain = normalize_domain(email.split("@")[-1])
                if not domain or domain in SKIP_EMAIL_DOMAINS:
                    continue
                if domain.endswith(".edu") or domain.endswith(".gov") or domain.endswith(".ac.uk"):
                    continue
                if prefer_domain:
                    prefer = normalize_domain(prefer_domain)
                    ok = (
                        domain == prefer
                        or domain.endswith("." + prefer)
                        or prefer.endswith("." + domain)
                        or _domains_related(domain, prefer or "")
                    )
                    if not ok:
                        continue
                out.append({
                    "email": email,
                    "first_name": first or None,
                    "last_name": last or None,
                    "affiliation": text[:240],
                    "title": (result.get("title") or "")[:200] or None,
                    "source": "europe_pmc",
                })
    return out


def search_author_emails(
    company: str,
    *,
    domain: str | None = None,
    extra_query: str | None = None,
    page_size: int = 25,
    max_emails: int = 20,
) -> list[dict]:
    """
    Search Europe PMC for authors affiliated with company.
    Returns [{email, first_name, last_name, affiliation, title, source}].
    """
    company = (company or "").strip()
    if not company:
        return []

    # AFF:"Pfizer" — optionally AND a topical term (de-identification = ICP signal)
    q = f'AFF:"{company}"'
    if extra_query:
        q = f"({q}) AND ({extra_query})"

    try:
        r = httpx.get(
            BASE,
            params={
                "query": q,
                "resultType": "core",
                "format": "json",
                "pageSize": page_size,
            },
            timeout=25.0,
            headers={"User-Agent": "JayAgents/1.0 (email pattern research)"},
        )
        if r.status_code != 200:
            logger.debug("Europe PMC %s for %s", r.status_code, company)
            return []
        results = ((r.json() or {}).get("resultList") or {}).get("result") or []
    except Exception as e:
        logger.warning("Europe PMC failed for %s: %s", company, e)
        return []

    prefer = (domain or "").lower().removeprefix("www.") or None
    found: list[dict] = []
    seen: set[str] = set()
    for res in results:
        for row in _affiliation_emails(res, prefer_domain=prefer):
            if row["email"] in seen:
                continue
            seen.add(row["email"])
            found.append(row)
            if len(found) >= max_emails:
                return found

    # Do NOT fall back to arbitrary emails in the affiliation text — that pulls
    # university co-author addresses (.edu) and invents wrong patterns.
    return found


def search_deidentification_authors(company: str, *, domain: str | None = None) -> list[dict]:
    """PMC search biased toward de-identification / anonymization authors — ICP signal."""
    return search_author_emails(
        company,
        domain=domain,
        extra_query='de-identification OR deidentification OR anonymiz* OR "synthetic data"',
        page_size=15,
        max_emails=10,
    )
