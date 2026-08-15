"""Account research briefs — retrieval artifact before any outreach writing.

One brief per company, assembled from signals, privacy footprint, filings,
and stored research. Generation should cite this — not invent context.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from .db import _now, get_connection, init_db, list_signals, normalize_company

logger = logging.getLogger(__name__)


def briefs_enabled() -> bool:
    return os.getenv("ACCOUNT_BRIEFS_ENABLED", "true").lower() not in ("0", "false", "no")


def ensure_briefs_table():
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                company TEXT NOT NULL,
                company_normalized TEXT NOT NULL,
                company_domain TEXT,
                brief_json TEXT NOT NULL,
                brief_text TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(agent, company_normalized)
            )
            """
        )
        conn.commit()


def assemble_account_brief(
    company: str,
    *,
    agent: str = "woodway",
    domain: str | None = None,
    force: bool = False,
) -> dict:
    """
    Build (or refresh) a structured account brief.
    Sources: DB signals, privacy footprint, lead rows, optional SEC emails/snippets.
    """
    if not briefs_enabled():
        return {"company": company, "sections": {}, "text": "", "skipped": True}

    ensure_briefs_table()
    key = normalize_company(company)
    if not force:
        cached = get_account_brief(company, agent=agent)
        if cached and cached.get("brief_text"):
            # Refresh if older than 14 days
            try:
                updated = datetime.fromisoformat(cached["updated_at"].replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - updated).days
                if age < int(os.getenv("ACCOUNT_BRIEF_TTL_DAYS", "14")):
                    return cached
            except Exception:
                pass

    sections: dict = {
        "company": company,
        "domain": domain,
        "signals": [],
        "privacy": {},
        "lead_facts": [],
        "filings": [],
        "sources": [],
    }

    # Signals
    for sig in list_signals(agent=agent, days=180, unconsumed_only=False, limit=20, company=company):
        sections["signals"].append({
            "id": sig.get("id"),
            "type": sig.get("signal_type"),
            "label": sig.get("label") or sig.get("signal_type"),
            "snippet": (sig.get("snippet") or "")[:300],
            "url": sig.get("source_url"),
            "strength": sig.get("strength"),
        })
        if sig.get("source_url"):
            sections["sources"].append(sig["source_url"])

    # Privacy footprint
    try:
        from .privacy_footprint import fetch_privacy_footprint
        fp = fetch_privacy_footprint(company, domain=domain)
        if fp:
            sections["privacy"] = {
                "dpo_name": fp.get("dpo_name"),
                "dpo_email": fp.get("dpo_email"),
                "mentions_deidentification": bool(fp.get("mentions_deidentification")),
                "policy_url": fp.get("policy_url") or fp.get("url"),
            }
            if fp.get("policy_url") or fp.get("url"):
                sections["sources"].append(fp.get("policy_url") or fp.get("url"))
    except Exception as e:
        logger.debug("Brief privacy fetch skipped: %s", e)

    # Existing lead rows (industry / signal text already stored)
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT industry, signal, contact_title, employee_count, company_domain
            FROM leads WHERE agent = ? AND company_normalized = ?
            ORDER BY updated_at DESC LIMIT 5
            """,
            (agent, key),
        ).fetchall()
    for row in rows:
        if row["industry"] or row["signal"]:
            sections["lead_facts"].append({
                "industry": row["industry"],
                "signal": (row["signal"] or "")[:240],
                "title_seen": row["contact_title"],
                "employees": row["employee_count"],
            })
        if not domain and row["company_domain"]:
            domain = row["company_domain"]
            sections["domain"] = domain

    # Light SEC / filing email scrape — only if domain known and enabled
    if domain and os.getenv("ACCOUNT_BRIEF_FILINGS", "false").lower() in ("1", "true", "yes"):
        try:
            from .sec_edgar import search_filing_emails
            filings = search_filing_emails(company, domain=domain, max_docs=3, max_emails=5)
            for f in (filings or [])[:5]:
                sections["filings"].append({
                    "email": f.get("email"),
                    "url": f.get("url") or f.get("source_url"),
                    "snippet": (f.get("snippet") or "")[:200],
                })
                if f.get("url") or f.get("source_url"):
                    sections["sources"].append(f.get("url") or f.get("source_url"))
        except Exception as e:
            logger.debug("Brief filings skipped: %s", e)

    text = format_brief_text(sections)
    record = {
        "company": company,
        "company_normalized": key,
        "company_domain": domain,
        "sections": sections,
        "brief_text": text,
        "updated_at": _now(),
    }
    save_account_brief(agent, record)
    return record


def format_brief_text(sections: dict) -> str:
    lines = [f"ACCOUNT BRIEF: {sections.get('company')}"]
    if sections.get("domain"):
        lines.append(f"Domain: {sections['domain']}")

    priv = sections.get("privacy") or {}
    if priv:
        lines.append(
            f"Privacy: de-id mentioned={'yes' if priv.get('mentions_deidentification') else 'no'}; "
            f"DPO={priv.get('dpo_name') or 'n/a'}"
        )

    sigs = sections.get("signals") or []
    if sigs:
        lines.append("Signals:")
        for s in sigs[:5]:
            lines.append(
                f"  - [{s.get('id')}] {s.get('label')}: {(s.get('snippet') or '')[:160]}"
            )

    facts = sections.get("lead_facts") or []
    if facts:
        lines.append("Prior facts:")
        for f in facts[:3]:
            if f.get("signal"):
                lines.append(f"  - {f['signal']}")

    filings = sections.get("filings") or []
    if filings:
        lines.append("Filings / published contacts:")
        for f in filings[:3]:
            lines.append(f"  - {f.get('email') or 'n/a'} {(f.get('snippet') or '')[:100]}")

    if not sigs and not priv and not facts and not filings:
        lines.append("(thin brief — no stored signals/privacy/filings yet)")

    return "\n".join(lines)


def save_account_brief(agent: str, record: dict) -> None:
    ensure_briefs_table()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO account_briefs (
                agent, company, company_normalized, company_domain,
                brief_json, brief_text, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent, company_normalized) DO UPDATE SET
              company = excluded.company,
              company_domain = excluded.company_domain,
              brief_json = excluded.brief_json,
              brief_text = excluded.brief_text,
              updated_at = excluded.updated_at
            """,
            (
                agent,
                record["company"],
                record["company_normalized"],
                record.get("company_domain"),
                json.dumps(record.get("sections") or {}),
                record.get("brief_text") or "",
                record.get("updated_at") or _now(),
            ),
        )
        conn.commit()


def get_account_brief(company: str, *, agent: str = "woodway") -> dict | None:
    ensure_briefs_table()
    key = normalize_company(company)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM account_briefs
            WHERE agent = ? AND company_normalized = ?
            """,
            (agent, key),
        ).fetchone()
    if not row:
        return None
    try:
        sections = json.loads(row["brief_json"] or "{}")
    except Exception:
        sections = {}
    return {
        "company": row["company"],
        "company_normalized": row["company_normalized"],
        "company_domain": row["company_domain"],
        "sections": sections,
        "brief_text": row["brief_text"],
        "updated_at": row["updated_at"],
    }


def brief_citation_ids(brief: dict | None) -> set[str]:
    """Allowed citation tokens from a brief (signal:<id>, brief, privacy)."""
    ids = {"brief"}
    if not brief:
        return ids
    sections = brief.get("sections") or {}
    if sections.get("privacy"):
        ids.add("privacy")
    for s in sections.get("signals") or []:
        if s.get("id") is not None:
            ids.add(f"signal:{s['id']}")
    return ids
