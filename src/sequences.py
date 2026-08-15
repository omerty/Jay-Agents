"""4-touch outreach sequences."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

from .db import _now, get_connection, init_db

logger = logging.getLogger(__name__)

DEFAULT_INTERVALS = [0, 4, 8, 16]


def sequences_enabled() -> bool:
    return os.getenv("SEQUENCE_ENABLED", "true").lower() not in ("0", "false", "no")


def sequence_intervals() -> list[int]:
    raw = os.getenv("SEQUENCE_INTERVALS", "")
    if raw:
        return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    return DEFAULT_INTERVALS


def save_sequence_steps(lead_id: int, steps: list[dict]):
    init_db()
    now = _now()
    with get_connection() as conn:
        conn.execute("DELETE FROM sequence_steps WHERE lead_id = ?", (lead_id,))
        for step in steps:
            conn.execute(
                """
                INSERT INTO sequence_steps (
                    lead_id, step_number, channel, scheduled_for, status, content, subject, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_id,
                    step["step_number"],
                    step["channel"],
                    step.get("scheduled_for"),
                    step.get("status", "pending"),
                    step.get("content"),
                    step.get("subject"),
                    now,
                ),
            )
        conn.commit()


def get_sequence_steps(lead_id: int) -> list[dict]:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sequence_steps WHERE lead_id = ? ORDER BY step_number",
            (lead_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def generate_sequence(
    config: dict,
    lead: dict,
    qualification: dict,
    *,
    touch1: dict | None = None,
    linkedin_note: str | None = None,
    use_llm: bool = True,
    research_context: str | None = None,
) -> list[dict]:
    """Build 4-touch sequence: email, LinkedIn, bump, breakup."""
    if not sequences_enabled():
        return []

    from .outreach import generate_linkedin_note, generate_outreach, generate_sequence_touch

    intervals = sequence_intervals()
    today = date.today()
    prospect = lead.get("prospect") or lead.get("company", "")
    contact = {
        "contact_name": lead.get("contact_name"),
        "contact_title": lead.get("contact_title"),
        "email": lead.get("email"),
        "company": lead.get("company"),
        "linkedin_url": lead.get("linkedin_url"),
    }

    steps: list[dict] = []

    # Touch 1 — trigger-anchored email
    if touch1:
        t1_body = touch1.get("body", "")
        t1_subj = touch1.get("subject", "Quick question")
    else:
        t1 = generate_outreach(
            config, prospect, qualification, use_llm=use_llm,
            research_context=research_context, contact=contact,
        )
        t1_body = t1.get("body", "")
        t1_subj = t1.get("subject", "Quick question")

    steps.append({
        "step_number": 1,
        "channel": "email",
        "scheduled_for": today.isoformat(),
        "status": "drafted",
        "subject": t1_subj,
        "content": t1_body,
    })

    # Touch 2 — LinkedIn
    li = linkedin_note or generate_linkedin_note(
        config, prospect, qualification, contact=contact,
        use_llm=use_llm, research_context=research_context,
    )
    steps.append({
        "step_number": 2,
        "channel": "linkedin",
        "scheduled_for": (today + timedelta(days=intervals[1] if len(intervals) > 1 else 4)).isoformat(),
        "status": "pending",
        "content": li,
    })

    # Touch 3 — bump
    bump = generate_sequence_touch(
        config, prospect, qualification, touch=3,
        contact=contact, use_llm=use_llm, research_context=research_context,
    )
    steps.append({
        "step_number": 3,
        "channel": "email",
        "scheduled_for": (today + timedelta(days=intervals[2] if len(intervals) > 2 else 8)).isoformat(),
        "status": "pending",
        "subject": bump.get("subject", "Re: quick follow-up"),
        "content": bump.get("body", ""),
    })

    # Touch 4 — breakup / referral ask
    breakup = generate_sequence_touch(
        config, prospect, qualification, touch=4,
        contact=contact, use_llm=use_llm, research_context=research_context,
    )
    steps.append({
        "step_number": 4,
        "channel": "email",
        "scheduled_for": (today + timedelta(days=intervals[3] if len(intervals) > 3 else 16)).isoformat(),
        "status": "pending",
        "subject": breakup.get("subject", "Should I close this out?"),
        "content": breakup.get("body", ""),
    })

    return steps


def halt_sequence(lead_id: int, *, reason: str = "halted") -> int:
    """Mark all pending sequence steps as skipped."""
    init_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE sequence_steps
            SET status = 'skipped'
            WHERE lead_id = ? AND status = 'pending'
            """,
            (lead_id,),
        )
        conn.commit()
        return cur.rowcount


def get_due_sequence_steps(*, agent: str | None = None, limit: int = 50) -> list[dict]:
    """Pending steps whose scheduled_for <= today, lead not replied/suppressed."""
    init_db()
    today = date.today().isoformat()
    q = """
        SELECT s.*, l.agent, l.email, l.contact_name, l.company, l.status AS lead_status,
               l.outreach_subject, l.gmail_draft_id, l.contact_tier, l.score,
               l.linkedin_url, l.mail_provider
        FROM sequence_steps s
        JOIN leads l ON l.id = s.lead_id
        WHERE s.status = 'pending'
          AND s.scheduled_for <= ?
          AND l.status NOT IN ('replied', 'skipped')
          AND COALESCE(l.reply_class, '') NOT IN ('negative', 'bounce')
    """
    params: list = [today]
    if agent:
        q += " AND l.agent = ?"
        params.append(agent)
    q += " ORDER BY s.scheduled_for ASC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def mark_step_status(step_id: int, status: str, *, draft_id: str | None = None) -> None:
    init_db()
    with get_connection() as conn:
        if draft_id:
            # store draft id in content prefix if no dedicated column — skip
            pass
        conn.execute(
            "UPDATE sequence_steps SET status = ? WHERE id = ?",
            (status, step_id),
        )
        conn.commit()


def advance_due_sequences(
    *,
    agent: str = "woodway",
    limit: int = 25,
    create_drafts: bool = True,
    on_progress=None,
) -> dict:
    """
    Create mailbox drafts (or LinkedIn queue notes) for due sequence steps.
    Touch 1 is usually already drafted at process time — this advances 2–4.

    Hard rule: follow-ups are drafts only. Never call send_message / send_draft
    from this timer-driven path.
    """
    emit = on_progress or (lambda msg: None)
    if not sequences_enabled():
        return {"advanced": 0, "skipped": True}

    from .contact_tier import tier_allows_email_draft
    from .db import set_lead_fields
    # create_draft only — send_* must stay unreachable from sequences.
    from .gmail_api import GmailError, create_draft as gmail_create, gmail_status
    from .microsoft_mail_api import MicrosoftMailError, create_draft as ms_create, microsoft_status

    prefer_ms = agent == "woodway"
    ms_ok = microsoft_status()["connected"]
    gmail_ok = gmail_status()["connected"]
    use_ms = (prefer_ms and ms_ok) or (ms_ok and not gmail_ok)
    use_gmail = (not use_ms) and gmail_ok

    due = get_due_sequence_steps(agent=agent, limit=limit)
    advanced = failed = linkedin_queued = 0

    for step in due:
        channel = (step.get("channel") or "email").lower()
        lead_id = step["lead_id"]
        step_id = step["id"]

        if channel == "linkedin":
            # Persist note onto lead for dashboard / manual send
            note = step.get("content") or ""
            if note:
                set_lead_fields(lead_id, linkedin_note=note)
            mark_step_status(step_id, "queued")
            linkedin_queued += 1
            emit(f"Sequence LinkedIn queued for lead {lead_id}")
            continue

        if channel != "email":
            mark_step_status(step_id, "skipped")
            continue

        email = (step.get("email") or "").strip()
        body = (step.get("content") or "").strip()
        subject = (step.get("subject") or "Follow-up").strip()
        tier = (step.get("contact_tier") or "B").upper()

        if not email or not body or not tier_allows_email_draft(tier):
            mark_step_status(step_id, "skipped")
            continue
        if not create_drafts or (not use_ms and not use_gmail):
            mark_step_status(step_id, "pending")  # leave for later
            continue

        try:
            if use_ms:
                result = ms_create(email, subject, body)
                provider = "microsoft"
            else:
                result = gmail_create(email, subject, body)
                provider = "gmail"
            set_lead_fields(
                lead_id,
                gmail_draft_id=result["draft_id"],
                gmail_thread_id=result.get("thread_id"),
                gmail_message_id=result.get("message_id"),
                mail_provider=provider,
                outreach_subject=subject,
                outreach_body=body,
                status="drafted",
            )
            mark_step_status(step_id, "drafted")
            advanced += 1
            emit(f"Sequence touch {step.get('step_number')} drafted for lead {lead_id}")
        except (GmailError, MicrosoftMailError) as e:
            logger.warning("Sequence draft failed lead %s: %s", lead_id, e)
            failed += 1

    return {
        "advanced": advanced,
        "linkedin_queued": linkedin_queued,
        "failed": failed,
        "due": len(due),
    }
