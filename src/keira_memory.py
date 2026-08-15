"""Keira research memory — don't re-burn credits on the same company blindly."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from .db import get_connection, init_db, normalize_company

logger = logging.getLogger(__name__)

# Default: do not re-research for 90 days unless an event fires
DEFAULT_COOLDOWN_DAYS = int(os.getenv("KEIRA_RESEARCH_COOLDOWN_DAYS", "90"))


def memory_enabled() -> bool:
    return os.getenv("KEIRA_RESEARCH_MEMORY", "true").lower() not in ("0", "false", "no")


def ensure_memory_table() -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keira_research_memory (
                company_normalized TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                last_researched_at TEXT,
                next_research_at TEXT,
                fingerprint TEXT,
                research_priority INTEGER DEFAULT 0,
                meta_json TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_keira_memory_next "
            "ON keira_research_memory(next_research_at)"
        )
        conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def fingerprint_lead(lead: dict | None = None, *, signal: str | None = None) -> str:
    """Stable hash of research-relevant fields — change ⇒ worth re-checking."""
    lead = lead or {}
    blob = "|".join([
        str(lead.get("company") or ""),
        str(lead.get("contact_name") or ""),
        str(lead.get("contact_title") or ""),
        str(lead.get("employee_count") or ""),
        str(signal or lead.get("signal") or ""),
    ]).lower().strip()
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def get_memory(company: str) -> dict | None:
    if not memory_enabled():
        return None
    ensure_memory_table()
    key = normalize_company(company)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM keira_research_memory WHERE company_normalized = ?",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def remember_research(
    company: str,
    *,
    status: str,
    reason: str | None = None,
    cooldown_days: int | None = None,
    fingerprint: str | None = None,
    research_priority: int = 0,
    meta: dict | None = None,
    force_next: datetime | None = None,
) -> dict:
    """Upsert research memory row."""
    if not memory_enabled():
        return {"skipped": True}
    ensure_memory_table()
    key = normalize_company(company)
    days = cooldown_days if cooldown_days is not None else DEFAULT_COOLDOWN_DAYS
    now = _now()

    # Extend cooldown on repeat rejection (doubles up to 365d) so bad leads aren't re-burned
    prev = None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, meta_json, next_research_at FROM keira_research_memory "
            "WHERE company_normalized = ?",
            (key,),
        ).fetchone()
        prev = dict(row) if row else None

    reject_streak = 0
    if prev:
        try:
            prev_meta = json.loads(prev.get("meta_json") or "{}")
        except json.JSONDecodeError:
            prev_meta = {}
        reject_streak = int(prev_meta.get("reject_streak") or 0)
        if prev.get("status") in ("rejected", "do_not_research") and status in ("rejected", "do_not_research"):
            reject_streak += 1
        elif status not in ("rejected", "do_not_research"):
            reject_streak = 0

    meta = dict(meta or {})
    meta["reject_streak"] = reject_streak

    if force_next:
        next_at = force_next
    elif status in ("do_not_research", "rejected"):
        # Base 180d; each repeat rejection extends (×1.5) capped at 365d
        base = max(days, 180)
        if reject_streak > 0:
            base = min(365, int(base * (1.5 ** min(reject_streak, 4))))
        next_at = now + timedelta(days=base)
    elif status == "qualified":
        next_at = now + timedelta(days=max(days, 120))
    elif status == "research_later":
        # Thin early reject: shorter hold so new signals can reopen (≤45d)
        next_at = now + timedelta(days=min(days, 45))
    else:
        next_at = now + timedelta(days=days)

    payload = {
        "company_normalized": key,
        "company": company,
        "status": status,
        "reason": (reason or "")[:500],
        "last_researched_at": _iso(now),
        "next_research_at": _iso(next_at),
        "fingerprint": fingerprint,
        "research_priority": int(research_priority or 0),
        "meta_json": json.dumps(meta),
        "updated_at": _iso(now),
    }
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO keira_research_memory (
                company_normalized, company, status, reason,
                last_researched_at, next_research_at, fingerprint,
                research_priority, meta_json, updated_at
            ) VALUES (
                :company_normalized, :company, :status, :reason,
                :last_researched_at, :next_research_at, :fingerprint,
                :research_priority, :meta_json, :updated_at
            )
            ON CONFLICT(company_normalized) DO UPDATE SET
                company=excluded.company,
                status=excluded.status,
                reason=excluded.reason,
                last_researched_at=excluded.last_researched_at,
                next_research_at=excluded.next_research_at,
                fingerprint=excluded.fingerprint,
                research_priority=excluded.research_priority,
                meta_json=excluded.meta_json,
                updated_at=excluded.updated_at
            """,
            payload,
        )
        conn.commit()
    return payload


def should_skip_research(
    company: str,
    *,
    fingerprint: str | None = None,
    now: datetime | None = None,
) -> dict:
    """
    Return {skip: bool, reason, memory}.
    Skip if cooldown active AND fingerprint unchanged.
    Never skip if fingerprint changed (event / new evidence).
    """
    if not memory_enabled():
        return {"skip": False, "reason": "memory_disabled", "memory": None}
    mem = get_memory(company)
    if not mem:
        return {"skip": False, "reason": "unknown", "memory": None}

    now = now or _now()
    if mem.get("status") == "do_not_research":
        # Still allow if fingerprint changed dramatically
        if fingerprint and mem.get("fingerprint") and fingerprint != mem["fingerprint"]:
            return {"skip": False, "reason": "fingerprint_changed", "memory": mem}
        return {"skip": True, "reason": "do_not_research", "memory": mem}

    next_raw = mem.get("next_research_at")
    if next_raw:
        try:
            nxt = datetime.fromisoformat(str(next_raw).replace("Z", "+00:00"))
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            if now < nxt:
                if fingerprint and mem.get("fingerprint") and fingerprint != mem["fingerprint"]:
                    return {"skip": False, "reason": "fingerprint_changed", "memory": mem}
                return {
                    "skip": True,
                    "reason": f"cooldown until {next_raw}",
                    "memory": mem,
                }
        except ValueError:
            pass
    return {"skip": False, "reason": "due", "memory": mem}


def companies_due(*, limit: int = 50, now: datetime | None = None) -> list[dict]:
    """Companies whose next_research_at has passed."""
    if not memory_enabled():
        return []
    ensure_memory_table()
    now = now or _now()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM keira_research_memory
            WHERE next_research_at IS NULL OR next_research_at <= ?
            ORDER BY research_priority DESC, next_research_at ASC
            LIMIT ?
            """,
            (_iso(now), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def status_from_qualification(q: dict, *, critic: dict | None = None) -> tuple[str, str]:
    """Map qualify/critic outcome → memory status + reason."""
    if q.get("rejected") or (critic or {}).get("hard_reject"):
        return "rejected", "; ".join((q.get("reject_reasons") or [])[:2]) or "rejected"
    critic = critic or {}
    if critic.get("approved_for_draft") or (
        critic.get("approved") and critic.get("recommendation") == "enrich_and_draft" and q.get("outreach_ok")
    ):
        return "qualified", q.get("why_now") or "critic draft-approved"
    if critic.get("approved_for_enrich") or critic.get("recommendation") == "enrich":
        return "qualified", "soft enrich — contacts ok, draft needs stronger succession"
    if critic.get("approved") and q.get("outreach_ok"):
        return "qualified", q.get("why_now") or "critic approved"
    if q.get("status") == "research_required" or critic.get("recommendation") == "research_required":
        return "research_later", "; ".join((q.get("why_not") or [])[:2]) or "research further"
    if not q.get("outreach_ok"):
        return "research_later", "not ready for outreach"
    return "watchlist", q.get("recommendation") or "watchlist"
