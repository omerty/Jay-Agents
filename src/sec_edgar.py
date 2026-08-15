"""SEC EDGAR — free filing emails for US public companies (no API key)."""

from __future__ import annotations

import logging
import re
import time
from functools import lru_cache

import httpx

from .email_patterns import normalize_domain

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
UA = "JayAgents/1.0 (email pattern research; research@jayagents.dev)"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

ROLE_LOCALS = frozenset({
    "noreply", "no-reply", "donotreply", "webmaster", "postmaster", "abuse",
    "corporate.secretary", "secretary", "ir", "investorrelations", "investor.relations",
    "media", "press", "info", "contact", "support", "privacy", "dpo", "gdpr",
    "doi_reporting", "reporting", "filings", "edgar", "sec", "notices",
})


@lru_cache(maxsize=1)
def _tickers() -> dict[str, dict]:
    try:
        r = httpx.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=HEADERS,
            timeout=30.0,
        )
        if r.status_code != 200:
            return {}
        return {str(k): v for k, v in (r.json() or {}).items()}
    except Exception as e:
        logger.warning("SEC tickers fetch failed: %s", e)
        return {}


def lookup_cik(company: str) -> tuple[str, str] | None:
    """Return (cik_10, title) for best ticker match."""
    needle = (company or "").lower().strip()
    if not needle:
        return None
    # Normalize common shortenings
    aliases = {
        "jpmorgan": "jpmorgan chase",
        "jp morgan": "jpmorgan chase",
        "citi": "citigroup",
        "bofa": "bank of america",
        "bank of america": "bank of america corp",
        "wells fargo": "wells fargo",
        "goldman": "goldman sachs",
        "goldman sachs": "goldman sachs group",
        "morgan stanley": "morgan stanley",
        "prudential": "prudential financial",
    }
    needle = aliases.get(needle, needle)

    best = None
    best_score = 0
    for row in _tickers().values():
        title = (row.get("title") or "").lower()
        ticker = (row.get("ticker") or "").lower()
        score = 0
        if title == needle or ticker == needle:
            score = 100
        elif title.startswith(needle) or needle in title:
            score = 50 + max(0, 30 - abs(len(title) - len(needle)))
        if score > best_score:
            best_score = score
            best = row
    if not best or best_score < 40:
        return None
    cik = str(best["cik_str"]).zfill(10)
    return cik, best.get("title") or company


def _domain_variants(domain: str | None, company: str) -> list[str]:
    out: list[str] = []
    d = normalize_domain(domain)
    if d:
        out.append(d)
    # Common bank domain aliases when Clearbit domain differs from filing emails
    key = company.lower()
    extras = {
        "jpmorgan": ["jpmorgan.com", "jpmchase.com", "jpmorganchase.com"],
        "citigroup": ["citi.com", "citigroup.com"],
        "citi": ["citi.com", "citigroup.com"],
        "bank of america": ["bofa.com", "bankofamerica.com"],
        "wells fargo": ["wellsfargo.com"],
        "goldman": ["gs.com", "goldmansachs.com"],
        "morgan stanley": ["morganstanley.com", "ms.com"],
    }
    for prefix, domains in extras.items():
        if prefix in key:
            out.extend(domains)
    # dedupe
    seen = set()
    uniq = []
    for x in out:
        x = normalize_domain(x)
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _fetch_text(url: str, *, max_bytes: int = 2_500_000) -> str | None:
    try:
        r = httpx.get(url, headers={**HEADERS, "Accept": "text/html,*/*"}, timeout=45.0, follow_redirects=True)
        if r.status_code >= 400:
            return None
        return r.content[:max_bytes].decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug("SEC doc fetch failed %s: %s", url, e)
        return None


def _personish(email: str) -> bool:
    local = email.split("@")[0].lower()
    if local in ROLE_LOCALS or any(local.startswith(r) for r in ("noreply", "no-reply")):
        return False
    if any(x in local for x in ("reporting", "noreply", "donotreply", "webmaster")):
        return False
    # Prefer first.last / first_last shapes — require a separator
    if re.match(r"^[a-z]+\.[a-z]+(?:\.[a-z]+)?$", local):
        return True
    if re.match(r"^[a-z]+_[a-z]+$", local):
        return True
    if re.match(r"^[a-z]\.[a-z]+$", local):
        return True
    return False


def search_filing_emails(
    company: str,
    *,
    domain: str | None = None,
    max_docs: int = 8,
    max_emails: int = 15,
) -> list[dict]:
    """
    Find corporate emails in recent SEC filings via EFTS + document fetch.
    Returns [{email, first_name, last_name, source, source_url}].
    """
    company = (company or "").strip()
    if not company:
        return []

    looked = lookup_cik(company)
    if not looked:
        logger.debug("No SEC CIK for %s", company)
        return []
    cik, title = looked
    domains = _domain_variants(domain, company)
    if not domains:
        return []

    q = " OR ".join(f'"@{d}"' for d in domains[:4])
    try:
        r = httpx.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={
                "q": q,
                "forms": "8-K,DEF 14A,10-K,SC 13D,SC 13G,EX-99",
                "dateRange": "custom",
                "startdt": "2019-01-01",
                "enddt": "2026-12-31",
                "ciks": cik,
            },
            headers=HEADERS,
            timeout=30.0,
        )
        if r.status_code != 200:
            logger.debug("SEC EFTS %s for %s", r.status_code, company)
            return []
        hits = ((r.json() or {}).get("hits") or {}).get("hits") or []
    except Exception as e:
        logger.warning("SEC EFTS failed for %s: %s", company, e)
        return []

    found: list[dict] = []
    seen: set[str] = set()
    for h in hits[:max_docs]:
        _id = h.get("_id") or ""
        if ":" not in _id:
            continue
        adsh, doc = _id.split(":", 1)
        acc = adsh.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
        text = _fetch_text(url)
        time.sleep(0.15)  # be polite to SEC
        if not text:
            continue
        for email in EMAIL_RE.findall(text):
            email = email.lower().rstrip(".")
            host = normalize_domain(email.split("@")[-1])
            if not host or not any(host == d or host.endswith("." + d) for d in domains):
                continue
            if email in seen or not _personish(email):
                continue
            seen.add(email)
            local = email.split("@")[0]
            parts = re.split(r"[._]", local)
            first = parts[0] if len(parts) >= 2 and parts[0].isalpha() else None
            last = parts[-1] if len(parts) >= 2 and parts[-1].isalpha() else None
            # Strip middle initials like nicholas.t.rapak → nicholas, rapak
            if first and last and len(parts) == 3 and len(parts[1]) == 1:
                last = parts[2] if parts[2].isalpha() else last
            found.append({
                "email": email,
                "first_name": first.title() if first else None,
                "last_name": last.title() if last else None,
                "source": "sec_edgar",
                "source_url": url,
                "company_title": title,
            })
            if len(found) >= max_emails:
                return found
    return found
