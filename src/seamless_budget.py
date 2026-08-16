"""Conservative Seamless research credit budgeting.

Search is free. Research (email enrich) costs ~1 credit per contact.
Hard stops before spend:
  - kill switch SEAMLESS_RESEARCH_ENABLED
  - per-run cap (agent-specific)
  - per-agent daily cap (SEAMLESS_DAILY_BUDGET_WOODWAY / _KEIRA)
  - shared daily + monthly budgets
  - optional API remaining reserve floor

Set SEAMLESS_CAPS_DISABLED=true to skip all credit caps (Keira/Woodway manual runs).
Usage is still recorded; only the spend clamps are lifted.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .db import create_notification, get_connection, get_notifications, init_db

logger = logging.getLogger(__name__)

# In-process dedupe so one run doesn't spam the bell / UI
_BUDGET_NOTIFY_KEYS: set[str] = set()

# Stable marker for UI job-log scanning
BUDGET_ALERT_MARKER = "[SEAMLESS_BUDGET]"

# High ceiling when caps are disabled (still bounds runaway allocate calls)
_UNCAPPED_LIMIT = 100_000


def caps_disabled() -> bool:
    """When true, allocate_research_slots does not clamp (manual runs only)."""
    raw = (os.getenv("SEAMLESS_CAPS_DISABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def format_budget_alert_message(reason: str, *, agent: str = "keira") -> str:
    reason = (reason or "Seamless research budget exhausted").strip()
    return f"{BUDGET_ALERT_MARKER} {agent}: {reason}"


def notify_seamless_budget_exhausted(
    reason: str,
    *,
    agent: str = "keira",
    context: str | None = None,
) -> dict:
    """Create a UI notification + structured alert when research is blocked mid-run.

    Dedupes per agent/day/reason so a single pipeline doesn't flood the bell.
    """
    agent_key = (agent or "keira").strip().lower()
    reason = (reason or "Seamless research budget exhausted").strip()
    msg = format_budget_alert_message(reason, agent=agent_key)
    if context:
        msg = f"{msg} ({context})"

    dedupe_key = f"{_day_key()}:{agent_key}:{reason[:120]}"
    alert = {
        "type": "seamless_budget_exhausted",
        "agent": agent_key,
        "reason": reason,
        "message": msg,
        "context": context,
        "notified": False,
    }
    if dedupe_key in _BUDGET_NOTIFY_KEYS:
        return alert

    # Also skip if an identical unread notification already exists today
    try:
        existing = get_notifications(unread_only=True, limit=30)
        today = _day_key()
        for n in existing:
            created = str(n.get("created_at") or "")
            if created.startswith(today) and BUDGET_ALERT_MARKER in str(n.get("message") or "") and agent_key in str(n.get("agent") or agent_key):
                if reason[:60] in str(n.get("message") or ""):
                    _BUDGET_NOTIFY_KEYS.add(dedupe_key)
                    return alert
    except Exception:
        pass

    try:
        create_notification(msg, agent=agent_key)
        _BUDGET_NOTIFY_KEYS.add(dedupe_key)
        alert["notified"] = True
        logger.warning("UI notified — Seamless budget exhausted for %s: %s", agent_key, reason)
    except Exception as e:
        logger.warning("Failed to create Seamless budget notification: %s", e)
    return alert


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seamless_usage (
            month TEXT PRIMARY KEY,
            credits_used INTEGER NOT NULL DEFAULT 0,
            last_credits_remaining INTEGER,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seamless_usage_daily (
            day TEXT PRIMARY KEY,
            credits_used INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seamless_usage_daily_agent (
            day TEXT NOT NULL,
            agent TEXT NOT NULL,
            credits_used INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (day, agent)
        )
    """)


def research_enabled() -> bool:
    """Kill switch — set SEAMLESS_RESEARCH_ENABLED=false to block all paid research."""
    return os.getenv("SEAMLESS_RESEARCH_ENABLED", "true").lower() not in (
        "0", "false", "no", "off",
    )


def monthly_budget() -> int:
    return int(os.getenv("SEAMLESS_MONTHLY_BUDGET", "200"))


def daily_budget() -> int:
    """Shared daily ceiling across all agents."""
    return int(os.getenv("SEAMLESS_DAILY_BUDGET", "25"))


def daily_budget_for_agent(agent: str) -> int:
    """
    Per-agent daily sub-allocation. Defaults split the shared daily budget:
      woodway 60% / keira 40% when agent-specific env unset.
    Never exceeds the shared SEAMLESS_DAILY_BUDGET.
    """
    shared = daily_budget()
    key = (agent or "").strip().lower()
    if key == "woodway":
        raw = os.getenv("SEAMLESS_DAILY_BUDGET_WOODWAY")
        if raw is not None and str(raw).strip() != "":
            return min(shared, int(raw))
        return min(shared, max(1, int(shared * 0.6)))
    if key == "keira":
        raw = os.getenv("SEAMLESS_DAILY_BUDGET_KEIRA")
        if raw is not None and str(raw).strip() != "":
            return min(shared, int(raw))
        return min(shared, max(1, int(shared * 0.4)))
    return shared


def min_api_reserve() -> int:
    """Stop researching if Seamless reports fewer than this many credits left."""
    return int(os.getenv("SEAMLESS_MIN_RESERVE", "50"))


def run_overage() -> int:
    """Extra research credits a run may spend past per-run / agent-daily caps (10–20)."""
    try:
        raw = int(os.getenv("SEAMLESS_RUN_OVERAGE", "20") or "20")
    except ValueError:
        raw = 20
    return max(10, min(20, raw))


def daily_hard_cap() -> int:
    """Absolute shared daily ceiling — overage cannot push a day toward 1000 credits."""
    raw = os.getenv("SEAMLESS_DAILY_HARD_CAP")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(daily_budget(), 250)


def max_research_per_run(agent: str = "keira") -> int:
    if caps_disabled():
        return _UNCAPPED_LIMIT
    if agent == "keira":
        return int(
            os.getenv("SEAMLESS_KEIRA_RESEARCH_LIMIT")
            or os.getenv("KEIRA_SEAMLESS_ENRICH_CAP")
            or "5"
        )
    if agent == "woodway":
        return int(os.getenv("SEAMLESS_WOODWAY_RESEARCH_LIMIT", "50"))
    return int(os.getenv("SEAMLESS_MAX_RESEARCH_PER_RUN", "5"))


def credits_used_this_month() -> int:
    init_db()
    with get_connection() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT credits_used FROM seamless_usage WHERE month = ?",
            (_month_key(),),
        ).fetchone()
    return int(row["credits_used"]) if row else 0


def credits_used_today() -> int:
    init_db()
    with get_connection() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT credits_used FROM seamless_usage_daily WHERE day = ?",
            (_day_key(),),
        ).fetchone()
    return int(row["credits_used"]) if row else 0


def credits_used_today_by_agent(agent: str) -> int:
    init_db()
    with get_connection() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT credits_used FROM seamless_usage_daily_agent WHERE day = ? AND agent = ?",
            (_day_key(), (agent or "").strip().lower()),
        ).fetchone()
    return int(row["credits_used"]) if row else 0


def _maybe_soft_alert(status: dict, *, agent: str) -> None:
    """Log when spend crosses 80% of daily/monthly/agent caps (once per check)."""
    daily = status.get("daily_budget") or 0
    used_today = status.get("credits_used_today") or 0
    if daily and used_today >= int(daily * 0.8):
        logger.warning(
            "Seamless shared daily budget ≥80%% — %s/%s used (agent=%s)",
            used_today, daily, agent,
        )
    agent_daily = status.get("agent_daily_budget") or 0
    agent_used = status.get("credits_used_today_agent") or 0
    if agent_daily and agent_used >= int(agent_daily * 0.8):
        logger.warning(
            "Seamless %s daily budget ≥80%% — %s/%s used",
            agent, agent_used, agent_daily,
        )
    monthly = status.get("monthly_budget") or 0
    used = status.get("credits_used") or 0
    if monthly and used >= int(monthly * 0.8):
        logger.warning(
            "Seamless monthly budget ≥80%% — %s/%s used (agent=%s)",
            used, monthly, agent,
        )


def record_credits_used(
    count: int,
    *,
    remaining: int | None = None,
    agent: str = "keira",
) -> dict:
    """Record research credits consumed (1 per researched contact). Call AFTER successful research."""
    if count <= 0:
        return budget_status(remaining, agent=agent)
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    month = _month_key()
    day = _day_key()
    agent_key = (agent or "keira").strip().lower()
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

        drow = conn.execute(
            "SELECT credits_used FROM seamless_usage_daily WHERE day = ?",
            (day,),
        ).fetchone()
        dused = (int(drow["credits_used"]) if drow else 0) + count
        if drow:
            conn.execute(
                "UPDATE seamless_usage_daily SET credits_used = ?, updated_at = ? WHERE day = ?",
                (dused, now, day),
            )
        else:
            conn.execute(
                "INSERT INTO seamless_usage_daily (day, credits_used, updated_at) VALUES (?, ?, ?)",
                (day, dused, now),
            )

        arow = conn.execute(
            "SELECT credits_used FROM seamless_usage_daily_agent WHERE day = ? AND agent = ?",
            (day, agent_key),
        ).fetchone()
        aused = (int(arow["credits_used"]) if arow else 0) + count
        if arow:
            conn.execute(
                """UPDATE seamless_usage_daily_agent
                   SET credits_used = ?, updated_at = ? WHERE day = ? AND agent = ?""",
                (aused, now, day, agent_key),
            )
        else:
            conn.execute(
                """INSERT INTO seamless_usage_daily_agent (day, agent, credits_used, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (day, agent_key, aused, now),
            )
        conn.commit()
    status = budget_status(remaining, agent=agent)
    _maybe_soft_alert(status, agent=agent_key)
    return status


def budget_status(remaining_header: int | None = None, *, agent: str = "keira") -> dict:
    budget = monthly_budget()
    daily = daily_budget()
    hard_daily = daily_hard_cap()
    agent_daily = daily_budget_for_agent(agent)
    used = credits_used_this_month()
    used_today = credits_used_today()
    used_today_agent = credits_used_today_by_agent(agent)
    monthly_left = max(0, budget - used)
    daily_left = max(0, daily - used_today)
    hard_daily_left = max(0, hard_daily - used_today)
    agent_daily_left = max(0, agent_daily - used_today_agent)
    return {
        "research_enabled": research_enabled(),
        "monthly_budget": budget,
        "daily_budget": daily,
        "daily_hard_cap": hard_daily,
        "agent_daily_budget": agent_daily,
        "run_overage": run_overage(),
        "credits_used": used,
        "credits_used_today": used_today,
        "credits_used_today_agent": used_today_agent,
        "credits_remaining_budget": monthly_left,
        "credits_remaining_today": daily_left,
        "credits_remaining_today_hard": hard_daily_left,
        "credits_remaining_today_agent": agent_daily_left,
        "api_credits_remaining": remaining_header,
        "min_api_reserve": min_api_reserve(),
        "per_run_limit": max_research_per_run(agent),
        "soft_warning": bool(
            (budget and used >= int(budget * 0.8))
            or (daily and used_today >= int(daily * 0.8))
            or (agent_daily and used_today_agent >= int(agent_daily * 0.8))
        ),
    }


def _hard_block_reason(status: dict, *, api_remaining: int | None) -> str:
    if not status.get("research_enabled"):
        return "Seamless research disabled (SEAMLESS_RESEARCH_ENABLED=false)"
    if status["credits_remaining_budget"] <= 0:
        return (
            f"Monthly Seamless budget exhausted "
            f"({status['credits_used']}/{status['monthly_budget']} used)"
        )
    if status["credits_remaining_today_hard"] <= 0:
        return (
            f"Shared daily Seamless hard cap exhausted "
            f"({status['credits_used_today']}/{status['daily_hard_cap']} used today)"
        )
    if api_remaining is not None and api_remaining < min_api_reserve() + 1:
        return (
            f"Seamless API reserve floor — only {api_remaining} credits left "
            f"(keeping {min_api_reserve()} in reserve)"
        )
    return "no Seamless research budget remaining"


def can_research(count: int, *, agent: str = "keira", api_remaining: int | None = None) -> tuple[bool, str]:
    """Pre-spend gate — call BEFORE issuing Seamless research API."""
    if not research_enabled():
        return False, "Seamless research disabled (SEAMLESS_RESEARCH_ENABLED=false)"
    if count <= 0:
        return True, ""
    allowed, reason = allocate_research_slots(count, agent=agent, api_remaining=api_remaining)
    if allowed >= count:
        return True, ""
    return False, reason or "no Seamless research budget remaining"


def allocate_research_slots(
    requested: int,
    *,
    agent: str = "keira",
    api_remaining: int | None = None,
) -> tuple[int, str]:
    """
    Return how many research credits we're allowed to spend right now.
    Per-run / agent-daily caps may overshoot by SEAMLESS_RUN_OVERAGE (10–20).
    Never exceeds monthly remaining or SEAMLESS_DAILY_HARD_CAP.
    """
    requested = max(0, int(requested or 0))
    if requested == 0:
        return 0, "nothing requested"
    if not research_enabled():
        return 0, "Seamless research disabled (SEAMLESS_RESEARCH_ENABLED=false)"
    if caps_disabled():
        allowed = min(requested, _UNCAPPED_LIMIT)
        if api_remaining is not None:
            allowed = min(allowed, max(0, api_remaining - min_api_reserve()))
        if allowed <= 0:
            return 0, "Seamless API remaining below reserve (caps disabled otherwise)"
        if allowed < requested:
            return allowed, f"capped to {allowed} by API reserve only (requested {requested})"
        return allowed, "caps disabled (SEAMLESS_CAPS_DISABLED)"

    status = budget_status(api_remaining, agent=agent)
    _maybe_soft_alert(status, agent=agent)
    overage = int(status["run_overage"])
    per_run = int(status["per_run_limit"])
    monthly_left = int(status["credits_remaining_budget"])
    hard_daily_left = int(status["credits_remaining_today_hard"])
    agent_left = int(status["credits_remaining_today_agent"])
    shared_left = int(status["credits_remaining_today"])
    agent_daily = int(status["agent_daily_budget"])
    used_agent = int(status["credits_used_today_agent"])
    used_today = int(status["credits_used_today"])
    daily = int(status["daily_budget"])

    strict = min(requested, per_run, agent_left, shared_left, monthly_left, hard_daily_left)
    if api_remaining is not None:
        strict = min(strict, max(0, api_remaining - min_api_reserve()))
    allowed = max(0, strict)
    note = ""

    if allowed < requested:
        # +10–20 past per-run / daily / agent slice — not a license to fill the hard cap
        agent_overage_left = max(0, agent_daily + overage - used_agent)
        shared_overage_left = max(0, daily + overage - used_today)
        overage_ceiling = min(
            requested,
            per_run + overage,
            agent_overage_left,
            shared_overage_left,
            hard_daily_left,
            monthly_left,
        )
        if api_remaining is not None:
            overage_ceiling = min(overage_ceiling, max(0, api_remaining - min_api_reserve()))
        overage_ceiling = max(0, overage_ceiling)
        if overage_ceiling > allowed:
            extra = overage_ceiling - allowed
            allowed = overage_ceiling
            note = (
                f"run overage +{extra} (limit {per_run}+{overage}; "
                f"daily hard {status['daily_hard_cap']})"
            )
            logger.warning("Seamless %s %s", agent, note)

    if allowed <= 0:
        return 0, _hard_block_reason(status, api_remaining=api_remaining)
    if allowed < requested:
        suffix = f" ({note})" if note else ""
        return allowed, f"capped to {allowed} by budget guards (requested {requested}){suffix}"
    if note:
        return allowed, note
    return allowed, ""
