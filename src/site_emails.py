"""Company website sweep — extract mailto: and inline emails (free, no key)."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import httpx

from .email_patterns import normalize_domain

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
MAILTO_RE = re.compile(r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", re.I)

SITE_PATHS = (
    "/",
    "/about",
    "/about-us",
    "/about/leadership",
    "/company",
    "/company/leadership",
    "/team",
    "/leadership",
    "/our-team",
    "/contact",
    "/contact-us",
    "/newsroom",
    "/media",
    "/press",
    "/investors",
    "/investor-relations",
    "/ir",
)

SKIP_LOCAL = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "webmaster", "postmaster", "abuse", "support", "helpdesk", "jobs",
    "careers", "hr", "recruiting", "newsletter", "subscribe",
    "queries", "msfs-queries", "customerservice", "customer.service",
    "webmaster", "privacy", "dpo", "gdpr", "media", "press", "ir",
})


def _looks_like_person_local(local: str) -> bool:
    local = (local or "").lower()
    if not local or local in SKIP_LOCAL:
        return False
    if any(x in local for x in ("query", "noreply", "support", "webmaster", "newsletter")):
        return False
    parts = re.split(r"[._\-]", local)
    if len(parts) >= 2 and all(p.isalpha() and len(p) > 1 for p in parts[:2]):
        return True
    return False


def _fetch(url: str, *, max_bytes: int = 200_000) -> str | None:
    try:
        r = httpx.get(
            url,
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": "JayAgents/1.0 (company email research)"},
        )
        if r.status_code >= 400:
            return None
        return r.content[:max_bytes].decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug("Site fetch failed %s: %s", url, e)
        return None


def _emails_from_html(html: str, *, domain: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in MAILTO_RE.findall(html):
        e = m.lower().strip()
        if e not in seen:
            seen.add(e)
            found.append(e)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    for m in EMAIL_RE.findall(text):
        e = m.lower().rstrip(".")
        if e not in seen:
            seen.add(e)
            found.append(e)

    domain = normalize_domain(domain) or domain
    kept = []
    for e in found:
        if "@" not in e:
            continue
        local, host = e.rsplit("@", 1)
        host = host.removeprefix("www.")
        if host != domain and not host.endswith("." + domain):
            continue
        if local.split("+")[0] in SKIP_LOCAL:
            continue
        if any(x in local for x in ("noreply", "no-reply", "donotreply")):
            continue
        if not _looks_like_person_local(local) and "." not in local and "_" not in local:
            # keep role aliases like privacy@ for DPO path, but pattern ladder filters them
            pass
        kept.append(e)
    return kept


def match_email_to_name(name: str, emails: list[str]) -> str | None:
    """Pick a site email whose local-part looks like First Last."""
    parts = [p.lower() for p in re.split(r"\s+", (name or "").strip()) if p.isalpha()]
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    if len(first) < 2 or len(last) < 2:
        return None
    for email in emails:
        local = (email or "").split("@")[0].lower()
        if first in local and last in local:
            return email
        compact = re.sub(r"[._\-]", "", local)
        if compact == f"{first[0]}{last}" or compact == f"{first}{last}":
            return email
        if local.startswith(f"{first}.{last}") or local.startswith(f"{first}_{last}"):
            return email
    return None


def sweep_company_site(domain: str, *, max_pages: int = 8) -> list[dict]:
    """
    Fetch common corporate pages and return emails on that domain.
    [{email, source_url, source}].
    """
    domain = normalize_domain(domain)
    if not domain:
        return []

    base = f"https://{domain}"
    out: list[dict] = []
    seen: set[str] = set()
    pages = 0

    for path in SITE_PATHS:
        if pages >= max_pages:
            break
        url = urljoin(base, path)
        html = _fetch(url)
        if not html:
            continue
        pages += 1
        for email in _emails_from_html(html, domain=domain):
            if email in seen:
                continue
            seen.add(email)
            out.append({
                "email": email,
                "source_url": url,
                "source": "site_sweep",
            })
    return out
