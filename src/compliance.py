"""Compliance — suppression, opt-out footer, DSAR, privacy posture, retention."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .db import add_suppression, init_db, _now, get_connection

# Formal retention schedule (Woodway buyers are privacy/governance teams —
# keep this explicit, not informal tribal knowledge).
RETENTION_POLICY = {
    "default_months": 18,
    "env": "RETENTION_MONTHS",
    "purge_statuses": (
        "discovered",
        "imported",
        "skipped",
        "awaiting_contact",
        "qualified",  # never drafted / never engaged
    ),
    "retain_statuses": (
        "drafted",
        "emailed",
        "replied",
    ),
    "notes": (
        "Unengaged prospects (no mailbox draft / no send / no reply) are purged "
        "after RETENTION_MONTHS. Drafted, emailed, and replied leads are retained "
        "for audit / conversation continuity until a DSAR erase request."
    ),
}


def retention_months() -> int:
    return int(os.getenv(RETENTION_POLICY["env"], str(RETENTION_POLICY["default_months"])))


def opt_out_footer(sender: str | None = None) -> str:
    if os.getenv("REQUIRE_OPT_OUT_FOOTER", "true").lower() in ("0", "false", "no"):
        return ""
    sender = sender or os.getenv("OUTREACH_SENDER_NAME", "Woodway Assurance")
    return (
        f"\n\n—\n{sender}\n"
        "If you'd prefer not to receive further messages, reply 'unsubscribe' "
        "and we'll remove you promptly."
    )


def dsar_export(email: str) -> dict:
    """Export all data held for a contact email."""
    init_db()
    email = email.strip().lower()
    with get_connection() as conn:
        leads = conn.execute(
            "SELECT * FROM leads WHERE LOWER(email) = ?", (email,)
        ).fetchall()
        evidence = conn.execute(
            "SELECT * FROM evidence WHERE entity_id IN (SELECT id FROM leads WHERE LOWER(email) = ?)",
            (email,),
        ).fetchall()
        outcomes = conn.execute(
            "SELECT * FROM outcomes WHERE lead_id IN (SELECT id FROM leads WHERE LOWER(email) = ?)",
            (email,),
        ).fetchall()
    return {
        "email": email,
        "exported_at": _now(),
        "leads": [dict(r) for r in leads],
        "evidence": [dict(r) for r in evidence],
        "outcomes": [dict(r) for r in outcomes],
    }


def dsar_delete(email: str) -> int:
    """Delete contact records for DSAR erasure."""
    init_db()
    email = email.strip().lower()
    deleted = 0
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM leads WHERE LOWER(email) = ?", (email,)).fetchall()
        for row in rows:
            lid = row["id"]
            conn.execute("DELETE FROM evidence WHERE entity_id = ?", (lid,))
            conn.execute("DELETE FROM outcomes WHERE lead_id = ?", (lid,))
            conn.execute("DELETE FROM sequence_steps WHERE lead_id = ?", (lid,))
            conn.execute("DELETE FROM leads WHERE id = ?", (lid,))
            deleted += 1
        conn.commit()
    add_suppression(email, scope="email", reason="DSAR erasure")
    return deleted


def privacy_posture_markdown() -> str:
    months = retention_months()
    purge = ", ".join(RETENTION_POLICY["purge_statuses"])
    retain = ", ".join(RETENTION_POLICY["retain_statuses"])
    return f"""# JayAgents Privacy Posture

Woodway Assurance uses JayAgents for B2B prospect research with privacy-respecting practices.

## Data sources
- Public web pages (privacy policies, news, job postings, LinkedIn public profiles)
- Business contact databases (Apollo, Hunter domain patterns) where licensed
- All extracted fields include source URL and snippet (evidence panel)

## Lawful basis
- B2B outreach to business roles using publicly available professional contact information
- CASL: sender identification and functioning opt-out in every message

## Contact provenance
Every lead shows which source, URL, and date justified the contact record.

## Suppression
Global suppression list — permanent, domain-level option. Checked before every draft.

## Retention schedule (formal)
- Default: **{months} months** (`{RETENTION_POLICY['env']}`, default {RETENTION_POLICY['default_months']})
- Auto-purged statuses: `{purge}`
- Retained until DSAR: `{retain}`
- Policy note: {RETENTION_POLICY['notes']}
- Purge job: nightly via `python -m src.daily` → `purge_stale_contacts()`

## DSAR
Export or delete any contact record on request.

## Human review
All outreach is drafted for human review — nothing is sent automatically.

## Last updated
{_now()[:10]}
"""


def purge_stale_contacts(months: int | None = None) -> int:
    """Delete unengaged leads older than the retention window."""
    months = months or retention_months()
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).isoformat()
    statuses = RETENTION_POLICY["purge_statuses"]
    placeholders = ",".join("?" * len(statuses))
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            DELETE FROM leads
            WHERE status IN ({placeholders})
              AND updated_at < ?
              AND (emailed_at IS NULL OR emailed_at = '')
            """,
            (*statuses, cutoff),
        )
        conn.commit()
        return cur.rowcount
