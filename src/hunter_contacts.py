"""Contact discovery via Hunter domain-search — one credit per company domain."""

from __future__ import annotations

import logging
import os
import re
import time

from .contact_enrich import enrich_lead_contact_fields
from .db import build_prospect_string, get_connection, init_db, upsert_lead
from .discover import _should_skip
from .email_patterns import (
    infer_pattern_from_email,
    normalize_domain,
    resolve_company_domain,
    save_pattern,
    get_cached_pattern,
)
from .hunter_api import domain_search, hunter_available

logger = logging.getLogger(__name__)

PRIVACY_TITLE_RE = re.compile(
    r"privacy|data governance|data protection|information governance|"
    r"chief privacy|dpo|gdpr|compliance|data sharing|ai governance|"
    r"information security|ciso|records management|data ethics",
    re.I,
)

SENIORITY_RE = re.compile(
    r"\b(chief|vp|vice president|director|head|senior director|ciso|officer|manager)\b",
    re.I,
)

ROLE_ALIAS_RE = re.compile(
    r"^(privacy|dpo|dataprotection|data-protection|gdpr|datagovernance)@",
    re.I,
)


def _title_score(title: str, target_titles: list[str]) -> int:
    if not title:
        return 0
    t = title.lower()
    score = 0
    if PRIVACY_TITLE_RE.search(t):
        score += 40
    if SENIORITY_RE.search(t):
        score += 15
    for target in target_titles:
        tl = target.lower()
        if tl in t:
            score += 35
            break
        parts = [p for p in tl.split() if len(p) > 3]
        if sum(1 for p in parts if p in t) >= 2:
            score += 20
            break
    return score


def _parse_name_from_email(email: str) -> tuple[str, str] | None:
    local = email.split("@")[0]
    for sep in (".", "_", "-"):
        if sep in local:
            parts = [p for p in local.split(sep) if p and not p.isdigit()]
            if len(parts) >= 2:
                return parts[0].title(), parts[-1].title()
    return None


def pick_contacts_from_domain_data(
    data: dict,
    *,
    target_titles: list[str],
    max_contacts: int = 3,
) -> list[dict]:
    """Rank Hunter domain-search emails by privacy/governance title fit."""
    if not data:
        return []
    ranked: list[tuple[int, dict]] = []
    for row in data.get("emails") or []:
        email = (row.get("value") or "").strip().lower()
        if not email or "@" not in email:
            continue
        first = (row.get("first_name") or "").strip()
        last = (row.get("last_name") or "").strip()
        title = (row.get("position") or row.get("title") or "").strip()
        role_alias = bool(ROLE_ALIAS_RE.search(email))
        if not first and not last:
            parsed = _parse_name_from_email(email)
            if parsed:
                first, last = parsed
        if role_alias and (not first or not last):
            # privacy@ / dpo@ aliases — valuable even without a person name
            local = email.split("@")[0].replace(".", " ").replace("-", " ").replace("_", " ")
            name = " ".join(p.title() for p in local.split()) + " (Privacy Office)"
            title = title or "Data Protection / Privacy"
            ts = 55
        else:
            if not first or not last:
                continue
            name = f"{first} {last}".strip()
            if len(name.split()) < 2:
                continue
            ts = _title_score(title, target_titles)
            if role_alias:
                ts = max(ts, 50)
            # Accept strong privacy/seniority signals; keep bar low — yield over precision
            if ts < 15:
                continue
        conf = row.get("confidence") or 0
        ranked.append((ts + int(conf or 0), {
            "contact_name": name,
            "contact_title": title or None,
            "email": email,
            "email_verified": 1 if (row.get("verification") or {}).get("status") == "valid" else 0,
            "email_source": "hunter_domain",
            "contact_tier": "A",
            "confidence": conf,
        }))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in ranked[:max_contacts]]


def _company_has_contact(agent: str, company: str) -> bool:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM leads
            WHERE agent = ? AND company = ?
              AND contact_name IS NOT NULL AND TRIM(contact_name) != ''
            LIMIT 1
            """,
            (agent, company),
        ).fetchone()
    return bool(row)


def enrich_contacts_via_hunter(
    agent: str,
    company_names: list[str],
    *,
    config: dict,
    limit: int = 50,
    on_progress=None,
) -> dict:
    """Run Hunter domain-search for companies missing a named contact."""
    emit = on_progress or (lambda msg: None)
    if not hunter_available():
        return {"imported": 0, "updated": 0, "skipped": 0, "domains_searched": 0, "provider": "hunter"}

    from pathlib import Path

    import yaml

    agents_dir = Path(__file__).parent.parent / "agents"
    if not config:
        with open(agents_dir / agent / "config.yaml") as f:
            config = yaml.safe_load(f)

    titles = (
        (config.get("seamless") or {}).get("job_title")
        or (config.get("apollo") or {}).get("person_titles")
        or (config.get("icp") or {}).get("titles")
        or []
    )
    domain_limit = int(os.getenv("HUNTER_DOMAIN_LIMIT", "50"))
    delay = float(os.getenv("HUNTER_DOMAIN_DELAY", "0.3"))

    names = [n.strip() for n in company_names if n and n.strip()]
    emit(f"Hunter domain-search at {len(names)} companies (1 credit/domain)…")

    imported = updated = skipped = domains_searched = 0
    init_db()

    for i, company in enumerate(names, 1):
        if imported + updated >= limit:
            break
        if _should_skip(company, config):
            skipped += 1
            continue
        if _company_has_contact(agent, company):
            skipped += 1
            continue

        domain = resolve_company_domain(company)
        if not domain:
            skipped += 1
            continue

        emit(f"  Hunter [{i}/{len(names)}] {company} ({domain})…")
        data = domain_search(domain, limit=domain_limit)
        domains_searched += 1
        # Cache pattern from this response — do NOT call Hunter again
        if data and not get_cached_pattern(domain):
            pattern = data.get("pattern") or ""
            sample = None
            for row in data.get("emails") or []:
                sample = sample or row.get("value")
                if not pattern:
                    pattern = infer_pattern_from_email(
                        row.get("value") or "",
                        row.get("first_name"),
                        row.get("last_name"),
                    ) or ""
                if pattern:
                    break
            if pattern:
                save_pattern(
                    domain,
                    pattern=pattern,
                    confidence=float(data.get("confidence") or 0.7),
                    source="hunter",
                    sample_email=sample,
                )

        contacts = pick_contacts_from_domain_data(data or {}, target_titles=titles)
        if not contacts:
            skipped += 1
            if delay:
                time.sleep(delay)
            continue

        contact = contacts[0]
        record = {
            "company": company,
            "contact_name": contact["contact_name"],
            "contact_title": contact["contact_title"],
            "email": contact["email"],
            "email_verified": contact.get("email_verified"),
            "email_source": contact.get("email_source"),
            "contact_tier": contact.get("contact_tier"),
            "company_domain": normalize_domain(domain),
            "industry": None,
            "signal": f"Hunter domain-search — {contact.get('contact_title') or 'privacy/governance role'}",
            "source": "hunter_domain",
            "status": "imported",
        }
        record = enrich_lead_contact_fields(record, company)
        record["prospect"] = build_prospect_string(record)
        _, created = upsert_lead(record, agent=agent)
        if created:
            imported += 1
        else:
            updated += 1

        if delay:
            time.sleep(delay)

    emit(f"Hunter done — {imported} imported, {updated} updated, {domains_searched} domains searched")
    return {
        "provider": "hunter_domain",
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "domains_searched": domains_searched,
        "with_email": imported + updated,
    }
