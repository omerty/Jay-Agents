"""Conservative Seamless research credit budgeting."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .db import get_connection, init_db


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seamless_usage (
            month TEXT PRIMARY KEY,
            credits_used INTEGER NOT NULL DEFAULT 0,
            last_credits_remaining INTEGER,
            updated_at TEXT NOT NULL
        )
    """)


def monthly_budget() -> int:
    return int(os.getenv("SEAMLESS_MONTHLY_BUDGET", "10000"))


def max_research_per_run(agent: str = "keira") -> int:
    if agent == "keira":
        return int(os.getenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8"))
    return int(os.getenv("SEAMLESS_MAX_RESEARCH_PER_RUN", "10"))


def credits_used_this_month() -> int:
    init_db()
    with get_connection() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT credits_used FROM seamless_usage WHERE month = ?",
            (_month_key(),),
        ).fetchone()
    return int(row["credits_used"]) if row else 0


def record_credits_used(count: int, *, remaining: int | None = None) -> dict:
    """Record research credits consumed (1 per researched contact/company)."""
    if count <= 0:
        return budget_status(remaining)
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    month = _month_key()
    with get_connection() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT credits_used FROM seamless_usage WHERE month = ?",
            (month,),
        ).fetchone()
        used = (int(row["credits_used"]) if row else 0) + count
        if row:
            conn.execute(
                """UPDATE seamless_usage
                   SET credits_used = ?, last_credits_remaining = COALESCE(?, last_credits_remaining),
                       updated_at = ?
                   WHERE month = ?""",
                (used, remaining, now, month),
            )
        else:
            conn.execute(
                """INSERT INTO seamless_usage (month, credits_used, last_credits_remaining, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (month, used, remaining, now),
            )
        conn.commit()
    return budget_status(remaining)


def budget_status(remaining_header: int | None = None) -> dict:
    budget = monthly_budget()
    used = credits_used_this_month()
    return {
        "monthly_budget": budget,
        "credits_used": used,
        "credits_remaining_budget": max(0, budget - used),
        "api_credits_remaining": remaining_header,
        "per_run_limit": max_research_per_run("keira"),
    }


def can_research(count: int) -> tuple[bool, str]:
    status = budget_status()
    remaining = status["credits_remaining_budget"]
    per_run = status["per_run_limit"]
    if count > per_run:
        return False, f"Run limit is {per_run} research credits per search (conservative cap)"
    if count > remaining:
        return False, f"Monthly Seamless budget exhausted ({status['credits_used']}/{status['monthly_budget']} used)"
    return True, ""
