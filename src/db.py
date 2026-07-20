"""SQLite persistence for leads — dedup, status tracking, export."""

import csv
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("LEADS_DB_PATH", Path(__file__).parent.parent / "leads.db"))

STATUSES = ("discovered", "imported", "qualified", "drafted", "emailed", "replied", "skipped")
PROTECTED_STATUSES = frozenset({"emailed", "replied"})

_INIT_LOCK = threading.Lock()
_initialized_paths: set[str] = set()


def normalize_company(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"\s+(inc|ltd|llc|corp|corporation|limited)\.?$", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return n


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    key = str(DB_PATH)
    if key in _initialized_paths:
        return
    with _INIT_LOCK:
        if key in _initialized_paths:
            return
        with get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL DEFAULT 'woodway',
                company TEXT NOT NULL,
                company_normalized TEXT NOT NULL,
                contact_name TEXT,
                contact_title TEXT,
                email TEXT,
                linkedin_url TEXT,
                industry TEXT,
                employee_count INTEGER,
                signal TEXT,
                prospect TEXT,
                score INTEGER,
                tier TEXT,
                outreach_subject TEXT,
                outreach_body TEXT,
                qualification_json TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'discovered',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_normalized);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_agent ON leads(agent);

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT,
                lead_id INTEGER,
                message TEXT NOT NULL,
                read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                ok INTEGER NOT NULL DEFAULT 1,
                summary TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );
            """)
            _migrate(conn)
        _initialized_paths.add(key)


def _migrate(conn: sqlite3.Connection):
    """Additive migrations for databases created before new columns existed."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    if "qualification_json" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN qualification_json TEXT")
    for col in ("gmail_draft_id", "gmail_thread_id", "gmail_message_id", "emailed_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def company_seen(company: str, agent: str = "woodway") -> bool:
    init_db()
    key = normalize_company(company)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE company_normalized = ? AND agent = ? AND status != 'skipped'",
            (key, agent),
        ).fetchone()
    return row is not None


def upsert_lead(data: dict, agent: str = "woodway") -> tuple[int, bool]:
    """Insert or update a lead. Returns (lead_id, created)."""
    init_db()
    company = data["company"]
    key = normalize_company(company)
    now = _now()
    email = data.get("email") or ""

    with get_connection() as conn:
        existing = None
        # A lead that came from the DB carries its id — always match on it,
        # otherwise re-processing a no-email lead would insert a duplicate
        if data.get("id"):
            existing = conn.execute(
                "SELECT id FROM leads WHERE id = ?", (data["id"],)
            ).fetchone()
        if not existing and email:
            existing = conn.execute(
                "SELECT id FROM leads WHERE company_normalized = ? AND email = ? AND agent = ?",
                (key, email, agent),
            ).fetchone()
        if not existing and not email and data.get("contact_name"):
            existing = conn.execute(
                "SELECT id FROM leads WHERE company_normalized = ? AND agent = ? AND contact_name = ?",
                (key, agent, data["contact_name"]),
            ).fetchone()
        if not existing and not email and data.get("source") == "discover":
            existing = conn.execute(
                "SELECT id FROM leads WHERE company_normalized = ? AND agent = ? AND email IS NULL AND source = 'discover'",
                (key, agent),
            ).fetchone()

        fields = {
            "company": company,
            "company_normalized": key,
            "contact_name": data.get("contact_name"),
            "contact_title": data.get("contact_title"),
            "email": data.get("email"),
            "linkedin_url": data.get("linkedin_url"),
            "industry": data.get("industry"),
            "employee_count": data.get("employee_count"),
            "signal": data.get("signal"),
            "prospect": data.get("prospect"),
            "score": data.get("score"),
            "tier": data.get("tier"),
            "outreach_subject": data.get("outreach_subject"),
            "outreach_body": data.get("outreach_body"),
            "qualification_json": data.get("qualification_json"),
            "source": data.get("source", "discover"),
            "status": data.get("status", "discovered"),
            "updated_at": now,
        }

        if existing:
            lead_id = existing["id"]
            old_row = conn.execute(
                "SELECT status FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
            if old_row and old_row["status"] in PROTECTED_STATUSES:
                fields["status"] = old_row["status"]
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE leads SET {sets} WHERE id = ?",
                [*fields.values(), lead_id],
            )
            created = False
        else:
            fields["agent"] = agent
            fields["created_at"] = now
            cols = ", ".join(fields)
            placeholders = ", ".join("?" * len(fields))
            cur = conn.execute(
                f"INSERT INTO leads ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
            lead_id = cur.lastrowid
            created = True
        conn.commit()
    return lead_id, created


def get_leads(
    *,
    agent: str = "woodway",
    status: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    init_db()
    q = "SELECT * FROM leads WHERE agent = ?"
    params: list = [agent]
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY score DESC, updated_at DESC"
    if limit:
        q += " LIMIT ?"
        params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_leads_by_statuses(statuses: list[str], agent: str = "woodway", limit: int = 10) -> list[dict]:
    init_db()
    placeholders = ", ".join("?" * len(statuses))
    q = f"SELECT * FROM leads WHERE agent = ? AND status IN ({placeholders}) ORDER BY created_at ASC LIMIT ?"
    with get_connection() as conn:
        rows = conn.execute(q, [agent, *statuses, limit]).fetchall()
    return [dict(r) for r in rows]


def update_lead_status(lead_id: int, status: str):
    if status not in STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {', '.join(STATUSES)}")
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), lead_id),
        )
        conn.commit()


_LEAD_FIELD_WHITELIST = {
    "status", "email", "gmail_draft_id", "gmail_thread_id", "gmail_message_id", "emailed_at",
    "outreach_subject", "outreach_body",
}


def set_lead_fields(lead_id: int, **fields):
    """Update whitelisted lead columns (used by the Gmail integration)."""
    bad = set(fields) - _LEAD_FIELD_WHITELIST
    if bad:
        raise ValueError(f"Cannot set fields: {', '.join(sorted(bad))}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"Invalid status '{fields['status']}'")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
            [*fields.values(), _now(), lead_id],
        )
        conn.commit()


def get_lead(lead_id: int) -> dict | None:
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- notifications


def create_notification(message: str, *, agent: str | None = None, lead_id: int | None = None) -> int:
    init_db()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO notifications (agent, lead_id, message, read, created_at) VALUES (?, ?, ?, 0, ?)",
            (agent, lead_id, message, _now()),
        )
        conn.commit()
    return cur.lastrowid


def get_notifications(*, unread_only: bool = False, limit: int = 50) -> list[dict]:
    init_db()
    q = "SELECT * FROM notifications"
    if unread_only:
        q += " WHERE read = 0"
    q += " ORDER BY created_at DESC LIMIT ?"
    with get_connection() as conn:
        rows = conn.execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_notifications_read(ids: list[int] | None = None):
    """Mark specific notifications read, or all if ids is None."""
    init_db()
    with get_connection() as conn:
        if ids:
            placeholders = ", ".join("?" * len(ids))
            conn.execute(f"UPDATE notifications SET read = 1 WHERE id IN ({placeholders})", ids)
        else:
            conn.execute("UPDATE notifications SET read = 1")
        conn.commit()


# ---------------------------------------------------------------- run history


def record_run(kind: str, *, ok: bool, summary: str, started_at: str, finished_at: str | None = None) -> int:
    init_db()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO runs (kind, ok, summary, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
            (kind, 1 if ok else 0, summary, started_at, finished_at or _now()),
        )
        conn.commit()
    return cur.lastrowid


def last_run(kind: str | None = None) -> dict | None:
    init_db()
    q = "SELECT * FROM runs"
    params: list = []
    if kind:
        q += " WHERE kind = ?"
        params.append(kind)
    q += " ORDER BY finished_at DESC LIMIT 1"
    with get_connection() as conn:
        row = conn.execute(q, params).fetchone()
    return dict(row) if row else None


def stats(agent: str = "woodway") -> dict:
    init_db()
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads WHERE agent = ?", (agent,)).fetchone()[0]
        by_status = conn.execute(
            "SELECT status, COUNT(*) as n FROM leads WHERE agent = ? GROUP BY status",
            (agent,),
        ).fetchall()
        with_email = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND email IS NOT NULL AND email != ''",
            (agent,),
        ).fetchone()[0]
        linkedin_only = conn.execute(
            """SELECT COUNT(*) FROM leads WHERE agent = ?
               AND (email IS NULL OR email = '')
               AND linkedin_url IS NOT NULL AND linkedin_url != ''
               AND contact_name IS NOT NULL AND contact_name != ''""",
            (agent,),
        ).fetchone()[0]
    return {
        "total": total,
        "with_email": with_email,
        "linkedin_only": linkedin_only,
        "by_status": {r["status"]: r["n"] for r in by_status},
    }


def export_csv(path: str | Path, agent: str = "woodway") -> int:
    init_db()
    leads = get_leads(agent=agent)
    if not leads:
        return 0

    fieldnames = [
        "id", "company", "contact_name", "contact_title", "email", "linkedin_url",
        "industry", "employee_count", "score", "tier", "status", "source", "signal",
        "outreach_subject", "outreach_body", "qualification_json", "prospect",
        "created_at", "updated_at",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(leads)
    return len(leads)


def build_prospect_string(lead: dict) -> str:
    """Build a prospect description for LLM from lead fields."""
    name = lead.get("contact_name")
    title = lead.get("contact_title")
    company = lead.get("company", "")

    if name and title:
        return f"{name}, {title} at {company}"
    if title:
        return f"{title} at {company}"
    if lead.get("prospect"):
        return lead["prospect"]
    return f"VP Data Governance at {company}"
