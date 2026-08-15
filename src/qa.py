"""Human QA queue — accept/edit/reject drafts to feed few-shot memory."""

from __future__ import annotations

import json
import logging

from .db import _now, get_connection, get_lead, init_db, set_lead_fields
from .retrieval import append_journal

logger = logging.getLogger(__name__)


def ensure_qa_table():
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qa_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                agent TEXT NOT NULL,
                action TEXT NOT NULL,
                notes TEXT,
                edited_subject TEXT,
                edited_body TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def record_qa(
    lead_id: int,
    action: str,
    *,
    agent: str = "woodway",
    notes: str | None = None,
    edited_subject: str | None = None,
    edited_body: str | None = None,
) -> dict:
    """
    action: accept | edit | reject
    - accept: promote talking points into journal
    - edit: save edited copy onto lead + journal
    - reject: mark lead skipped / cold and journal why
    """
    ensure_qa_table()
    action = (action or "").lower().strip()
    if action not in ("accept", "edit", "reject"):
        return {"ok": False, "error": "action must be accept|edit|reject"}

    lead = get_lead(lead_id)
    if not lead:
        return {"ok": False, "error": "lead not found"}

    agent = agent or lead.get("agent") or "woodway"
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO qa_events (lead_id, agent, action, notes, edited_subject, edited_body, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, agent, action, notes, edited_subject, edited_body, _now()),
        )
        conn.commit()
        qa_id = cur.lastrowid

    if action == "edit" and (edited_subject or edited_body):
        patch = {}
        if edited_subject:
            patch["outreach_subject"] = edited_subject
        if edited_body:
            patch["outreach_body"] = edited_body
        set_lead_fields(lead_id, **patch)
        append_journal(
            agent,
            f"QA edit lead={lead_id} {lead.get('company')}: {(edited_subject or '')[:80]}",
            kind="qa_accept",
        )
    elif action == "accept":
        append_journal(
            agent,
            f"QA accept lead={lead_id} {lead.get('contact_name')} @ {lead.get('company')} "
            f"score={lead.get('score')} subj={lead.get('outreach_subject')}",
            kind="qa_accept",
        )
    elif action == "reject":
        set_lead_fields(lead_id, status="skipped", tier="cold")
        append_journal(
            agent,
            f"QA reject lead={lead_id} {lead.get('company')}: {notes or 'no reason'}",
            kind="qa_reject",
        )

    return {"ok": True, "qa_id": qa_id, "action": action}


def list_qa_queue(agent: str = "woodway", *, limit: int = 25) -> list[dict]:
    """Drafted leads awaiting human review (no QA accept yet preferred)."""
    ensure_qa_table()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT l.* FROM leads l
            WHERE l.agent = ? AND l.status = 'drafted'
              AND l.id NOT IN (
                SELECT lead_id FROM qa_events WHERE action = 'accept' AND agent = ?
              )
            ORDER BY COALESCE(l.priority_score, l.score, 0) DESC
            LIMIT ?
            """,
            (agent, agent, limit),
        ).fetchall()
    return [dict(r) for r in rows]
