"""Fetch privacy policy pages and extract DPO contacts + de-identification signals."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from .db import get_connection, init_db, _now
from .email_patterns import normalize_domain, resolve_company_domain
from .enrich import enrich_company

logger = logging.getLogger(__name__)

PRIVACY_PATHS = (
    "/privacy", "/privacy-policy", "/privacy_policy", "/legal/privacy",
    "/gdpr", "/data-protection", "/data-protection-policy", "/legal/data-protection",
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
DPO_RE = re.compile(
    r"(?:data protection officer|dpo|privacy officer)[:\s,]*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
    re.I,
)
DEID_SIGNALS = re.compile(
    r"de[- ]identif|anonymi[sz]|pseudonymi[sz]|secondary use|research use",
    re.I,
)


def _base_url(domain: str) -> str:
    d = normalize_domain(domain) or domain
    return f"https://{d}"


def _fetch_text(url: str, *, max_bytes: int = 120_000) -> str | None:
    try:
        r = httpx.get(
            url,
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": "JayAgents/1.0 (privacy research)"},
        )
        if r.status_code >= 400:
            return None
        content = r.content[:max_bytes]
        return content.decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug("Privacy fetch failed %s: %s", url, e)
        return None


def _extract_from_html(html: str, source_url: str) -> dict:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    emails = []
    for m in EMAIL_RE.finditer(text):
        e = m.group(0).lower()
        if any(x in e for x in ("example.com", "sentry", "wix", "wordpress")):
            continue
        if e not in emails:
            emails.append(e)

    dpo_name = None
    m = DPO_RE.search(text)
    if m:
        dpo_name = m.group(1).strip()

    privacy_emails = [e for e in emails if any(p in e for p in ("dpo@", "privacy@", "dataprotection@", "gdpr@"))]
    deid = bool(DEID_SIGNALS.search(text))

    snippet = text[:400].strip()
    return {
        "dpo_name": dpo_name,
        "dpo_email": privacy_emails[0] if privacy_emails else None,
        "privacy_emails": privacy_emails,
        "all_emails": emails[:8],
        "mentions_deidentification": deid,
        "source_url": source_url,
        "snippet": snippet,
    }


def get_cached_footprint(domain: str) -> dict | None:
    init_db()
    domain = normalize_domain(domain)
    if not domain:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM privacy_footprint WHERE domain = ?", (domain,)
        ).fetchone()
    return dict(row) if row else None


def save_footprint(domain: str, data: dict):
    init_db()
    domain = normalize_domain(domain)
    if not domain:
        return
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO privacy_footprint (
                domain, dpo_name, dpo_email, privacy_emails_json, mentions_deidentification,
                source_url, snippet, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
              dpo_name = excluded.dpo_name,
              dpo_email = excluded.dpo_email,
              privacy_emails_json = excluded.privacy_emails_json,
              mentions_deidentification = excluded.mentions_deidentification,
              source_url = excluded.source_url,
              snippet = excluded.snippet,
              fetched_at = excluded.fetched_at
            """,
            (
                domain,
                data.get("dpo_name"),
                data.get("dpo_email"),
                __import__("json").dumps(data.get("privacy_emails") or []),
                1 if data.get("mentions_deidentification") else 0,
                data.get("source_url"),
                (data.get("snippet") or "")[:500],
                _now(),
            ),
        )
        conn.commit()


def _extract_dpo_with_llm(text: str, company: str) -> dict | None:
    """Haiku extraction when regex misses DPO name/email in privacy page text."""
    if len(text) < 200:
        return None
    try:
        from .llm import chat_json
        from .llm_optimize import task_defaults, truncate_context

        defaults = task_defaults("extract")
        prompt = f"""Extract data protection / privacy contact from this privacy policy text for {company}.

TEXT:
{truncate_context(text[:8000], defaults['context_chars'])}

JSON only:
{{
  "dpo_name": "First Last or null",
  "dpo_email": "email or null",
  "mentions_deidentification": true/false
}}"""
        result = chat_json(
            prompt,
            system="Extract DPO/privacy contacts from policy text. JSON only.",
            max_tokens=256,
            temperature=0.1,
            task="extract",
        )
        name = (result.get("dpo_name") or "").strip()
        email = (result.get("dpo_email") or "").strip().lower()
        if not name and not email:
            return None
        return {
            "dpo_name": name or None,
            "dpo_email": email if "@" in email else None,
            "mentions_deidentification": bool(result.get("mentions_deidentification")),
        }
    except Exception as e:
        logger.debug("LLM DPO extract failed: %s", e)
        return None


def fetch_privacy_footprint(company: str, *, domain: str | None = None) -> dict | None:
    """Fetch and cache privacy disclosure for a company domain."""
    domain = normalize_domain(domain) or resolve_company_domain(company)
    if not domain:
        enrichment = enrich_company(company)
        domain = normalize_domain(enrichment.get("domain"))
    if not domain:
        return None

    cached = get_cached_footprint(domain)
    if cached:
        return cached

    base = _base_url(domain)
    best: dict | None = None
    for path in PRIVACY_PATHS:
        url = urljoin(base, path)
        html = _fetch_text(url)
        if not html:
            continue
        parsed = _extract_from_html(html, url)
        if parsed.get("dpo_email") or parsed.get("dpo_name") or parsed.get("mentions_deidentification"):
            best = parsed
            break
        if best is None:
            best = parsed

    if best and not best.get("dpo_name") and not best.get("dpo_email"):
        text = best.get("snippet") or ""
        if html := _fetch_text(best.get("source_url", "")):
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)[:12000]
        llm_hit = _extract_dpo_with_llm(text, company)
        if llm_hit:
            best.update({k: v for k, v in llm_hit.items() if v is not None})
            if llm_hit.get("mentions_deidentification"):
                best["mentions_deidentification"] = True

    if not best:
        return None

    save_footprint(domain, best)
    return get_cached_footprint(domain)


def footprint_qualification_boost(footprint: dict | None) -> tuple[int, str | None]:
    """Score boost when privacy policy mentions de-identification."""
    if not footprint:
        return 0, None
    if footprint.get("mentions_deidentification"):
        return 8, "Privacy policy mentions de-identification / anonymization"
    return 0, None


def contact_from_footprint(footprint: dict, company: str) -> dict | None:
    """Build a tier-A/B contact record from DPO disclosure."""
    dpo_name = footprint.get("dpo_name")
    dpo_email = footprint.get("dpo_email")
    if not dpo_name and not dpo_email:
        return None
    name = dpo_name or "Privacy Office"
    record = {
        "contact_name": name if " " in name else f"{name} (Privacy Office)",
        "contact_title": "Data Protection Officer",
        "email": dpo_email,
        "email_source": "privacy_dpo" if dpo_email else None,
        # Published DPO emails are still unverified until Hunter/MX check
        "email_verified": 0,
        "email_inferred": 0,
        "company_domain": footprint.get("domain") or normalize_domain(company),
        "signal": "DPO / privacy contact from public privacy policy",
    }
    if dpo_email:
        record["contact_tier"] = "B"
    else:
        record["contact_tier"] = "C"
    return record
