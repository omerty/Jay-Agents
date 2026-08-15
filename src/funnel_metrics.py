"""Per-run funnel metrics — discover → keep → contact → email → draft → reply."""

from __future__ import annotations

import json
import logging

from .db import _now, get_connection, init_db, save_checkpoint

logger = logging.getLogger(__name__)


def ensure_funnel_table():
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS funnel_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                run_id TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_funnel_agent ON funnel_metrics(agent, created_at)"
        )
        conn.commit()


def compute_funnel_from_summary(summary: dict) -> dict:
    """Derive rates from a pipeline summary dict (Woodway or Keira)."""
    if (summary.get("agent") or "").strip().lower() == "keira":
        return _keira_funnel_from_summary(summary)

    steps = summary.get("steps") or {}
    discovery = steps.get("discovery") or {}
    digest = steps.get("digest") or {}
    contacts = steps.get("contacts") or {}
    process = steps.get("process") or {}
    drafts = steps.get("drafts") or {}

    discovered = int(discovery.get("imported") or discovery.get("companies") or 0)
    kept = int(digest.get("kept") or len(digest.get("companies") or []))
    contact_hits = int(contacts.get("imported") or 0) + int(contacts.get("updated") or 0)
    with_email = int(contacts.get("with_email") or 0)
    processed = int(process.get("count") or 0)
    drafted = int(drafts.get("created") or 0)

    def rate(num: int, den: int) -> float | None:
        if den <= 0:
            return None
        return round(num / den, 3)

    return {
        "discovered": discovered,
        "kept": kept,
        "contacts": contact_hits,
        "with_email": with_email,
        "processed": processed,
        "drafted": drafted,
        "rates": {
            "discover_to_keep": rate(kept, max(discovered, kept)),
            "keep_to_contact": rate(contact_hits, kept),
            "contact_to_email": rate(with_email, contact_hits) if contact_hits else rate(with_email, kept),
            "process_to_draft": rate(drafted, processed),
        },
        "costs": summary.get("costs"),
        "run_id": summary.get("run_id"),
        "agent": summary.get("agent", "woodway"),
    }


def _keira_funnel_from_summary(summary: dict) -> dict:
    steps = summary.get("steps") or {}
    discovery = steps.get("discovery") or {}
    gates = steps.get("gates") or {}
    cheap = steps.get("cheap_email") or {}
    critic = steps.get("critic") or {}
    contacts = steps.get("contacts") or steps.get("enrich") or {}
    process = steps.get("process") or {}
    drafts = steps.get("drafts") or {}

    discovered = int(discovery.get("candidates") or 0)
    kept = int(gates.get("survivors") or 0)
    with_email = int(cheap.get("inferred_emails") or 0) + int(contacts.get("with_email") or 0)
    critic_enrich = int(critic.get("enrich_queue") or critic.get("approved_for_enrich") or 0)
    processed = int(process.get("count") or 0)
    drafted = int(drafts.get("created") or 0)

    def rate(num: int, den: int) -> float | None:
        if den <= 0:
            return None
        return round(num / den, 3)

    return {
        "discovered": discovered,
        "kept": kept,
        "with_owner": kept,
        "with_email": with_email,
        "critic_enrich": critic_enrich,
        "processed": processed,
        "drafted": drafted,
        "rates": {
            "discover_to_keep": rate(kept, max(discovered, kept)),
            "keep_to_email": rate(with_email, kept),
            "enrich_to_draft": rate(drafted, critic_enrich),
            "process_to_draft": rate(drafted, processed),
        },
        "run_id": summary.get("run_id"),
        "agent": "keira",
    }


def persist_funnel(agent: str, run_id: str, metrics: dict) -> int:
    ensure_funnel_table()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO funnel_metrics (agent, run_id, metrics_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (agent, run_id, json.dumps(metrics), _now()),
        )
        conn.commit()
        row_id = cur.lastrowid
    save_checkpoint(agent, run_id, "funnel", metrics)
    return int(row_id or 0)


def latest_funnel(agent: str = "woodway", *, limit: int = 10) -> list[dict]:
    ensure_funnel_table()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM funnel_metrics WHERE agent = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    out = []
    for r in rows:
        try:
            m = json.loads(r["metrics_json"])
        except Exception:
            m = {}
        out.append({"id": r["id"], "run_id": r["run_id"], "created_at": r["created_at"], **m})
    return out


def live_funnel_snapshot(agent: str = "woodway") -> dict:
    """Current DB funnel counts (not per-run)."""
    init_db()
    with get_connection() as conn:
        def count(status: str | None = None, extra: str = "") -> int:
            q = "SELECT COUNT(*) AS n FROM leads WHERE agent = ?"
            params: list = [agent]
            if status:
                q += " AND status = ?"
                params.append(status)
            if extra:
                q += f" AND {extra}"
            return int(conn.execute(q, params).fetchone()["n"])

        awaiting = count(extra="(contact_name IS NULL OR TRIM(contact_name) = '')")
        named = count(extra="contact_name IS NOT NULL AND TRIM(contact_name) != ''")
        emailed = count(extra="email IS NOT NULL AND TRIM(email) != ''")
        drafted = count("drafted")
        sent = count("emailed")
        replied = count("replied")

    return {
        "awaiting_contact": awaiting,
        "named_contacts": named,
        "with_email": emailed,
        "drafted": drafted,
        "emailed": sent,
        "replied": replied,
        "reply_rate": round(replied / sent, 3) if sent else None,
    }
