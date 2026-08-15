"""SQLite persistence for leads — dedup, status tracking, export."""

import csv
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("LEADS_DB_PATH", Path(__file__).parent.parent / "leads.db"))

STATUSES = (
    "discovered", "imported", "qualified", "drafted", "emailed", "replied",
    "awaiting_contact", "skipped",
)
PROTECTED_STATUSES = frozenset({"emailed", "replied"})

_INIT_LOCK = threading.Lock()
_initialized_paths: set[str] = set()


# Canonical keys for common duplicate company names (dedupe by alias + domain)
COMPANY_ALIASES: dict[str, str] = {
    "citigroup": "citi",
    "citibank": "citi",
    "citigroup inc": "citi",
    "mckesson corporation": "mckesson",
    "regeneron pharmaceuticals": "regeneron",
    "sanofi aventis": "sanofi",
    "oracle health": "cerner",
    "oracle cerner": "cerner",
}


def normalize_company(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"\s+(inc|ltd|llc|corp|corporation|limited)\.?$", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return COMPANY_ALIASES.get(n, n)


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

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE,
                otp_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_invites_email ON invites(email);

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            """)
            _migrate(conn)
        _initialized_paths.add(key)


def _migrate(conn: sqlite3.Connection):
    """Additive migrations for databases created before new columns existed."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    if "qualification_json" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN qualification_json TEXT")
    for col in ("gmail_draft_id", "gmail_thread_id", "gmail_message_id", "emailed_at", "mail_provider"):
        if col not in cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")
    for col, typ in (
        ("contact_tier", "TEXT"),
        ("email_inferred", "INTEGER"),
        ("email_verified", "INTEGER"),
        ("email_source", "TEXT"),
        ("company_domain", "TEXT"),
        ("linkedin_note", "TEXT"),
        ("reply_class", "TEXT"),
        ("revive_at", "TEXT"),
        ("priority_score", "REAL"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company_email_patterns (
            domain TEXT PRIMARY KEY,
            pattern TEXT NOT NULL,
            confidence REAL,
            source TEXT,
            sample_email TEXT,
            verified_count INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS privacy_footprint (
            domain TEXT PRIMARY KEY,
            dpo_name TEXT,
            dpo_email TEXT,
            privacy_emails_json TEXT,
            mentions_deidentification INTEGER DEFAULT 0,
            source_url TEXT,
            snippet TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS suppression (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL DEFAULT 'woodway',
            identifier TEXT,
            scope TEXT DEFAULT 'email',
            company_normalized TEXT,
            domain TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_suppression_agent ON suppression(agent);
        CREATE TABLE IF NOT EXISTS serp_cache (
            query_hash TEXT PRIMARY KEY,
            query_text TEXT NOT NULL,
            provider TEXT,
            results_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL DEFAULT 'woodway',
            company TEXT,
            company_domain TEXT,
            signal_type TEXT NOT NULL,
            strength REAL DEFAULT 1.0,
            detected_at TEXT NOT NULL,
            half_life_days INTEGER DEFAULT 90,
            source_url TEXT,
            snippet TEXT,
            consumed_at TEXT,
            lead_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_signals_agent ON signals(agent);
        CREATE INDEX IF NOT EXISTS idx_signals_domain ON signals(company_domain);
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            company TEXT,
            field TEXT NOT NULL,
            value TEXT,
            source_url TEXT,
            snippet TEXT,
            extracted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_entity ON evidence(entity_type, entity_id);
        CREATE TABLE IF NOT EXISTS sequence_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            channel TEXT NOT NULL,
            scheduled_for TEXT,
            draft_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            content TEXT,
            subject TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sequence_lead ON sequence_steps(lead_id);
        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            reply_class TEXT,
            revive_at TEXT,
            occurred_at TEXT NOT NULL,
            notes TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outcomes_lead ON outcomes(lead_id);
        CREATE TABLE IF NOT EXISTS run_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            step TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_costs_run ON run_costs(run_id);
        CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            run_id TEXT NOT NULL,
            step TEXT NOT NULL,
            summary_json TEXT,
            completed_at TEXT NOT NULL,
            UNIQUE(agent, run_id, step)
        );
        CREATE TABLE IF NOT EXISTS agent_cursors (
            agent TEXT NOT NULL,
            name TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (agent, name)
        );
        CREATE TABLE IF NOT EXISTS run_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL DEFAULT 'woodway',
            kind TEXT NOT NULL DEFAULT 'note',
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_journal_agent ON run_journal(agent);
    """)
    sup_cols = {row["name"] for row in conn.execute("PRAGMA table_info(suppression)")}
    if sup_cols:
        if "identifier" not in sup_cols:
            conn.execute("ALTER TABLE suppression ADD COLUMN identifier TEXT")
        if "scope" not in sup_cols:
            conn.execute("ALTER TABLE suppression ADD COLUMN scope TEXT DEFAULT 'email'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_suppression_identifier ON suppression(identifier)"
        )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def company_seen(company: str, agent: str = "woodway", *, domain: str | None = None) -> bool:
    """True if company or domain already exists (non-terminal rows)."""
    from .email_patterns import normalize_domain, resolve_company_domain

    init_db()
    key = normalize_company(company)
    domain = normalize_domain(domain) or resolve_company_domain(company)
    active = ("discovered", "imported", "qualified", "drafted", "emailed", "replied", "awaiting_contact")
    placeholders = ", ".join("?" * len(active))
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT 1 FROM leads
            WHERE agent = ? AND status IN ({placeholders})
              AND (company_normalized = ? OR (? IS NOT NULL AND company_domain = ?))
            LIMIT 1
            """,
            (agent, *active, key, domain, domain),
        ).fetchone()
    return row is not None


def upsert_lead(data: dict, agent: str = "woodway") -> tuple[int, bool]:
    """Insert or update a lead. Returns (lead_id, created)."""
    from .email_patterns import normalize_domain
    from .normalize import normalize_display_name

    init_db()
    company = normalize_display_name(data["company"], kind="company") or data["company"]
    data = {**data, "company": company}
    if data.get("contact_name"):
        data["contact_name"] = normalize_display_name(data["contact_name"], kind="person") or data["contact_name"]
    key = normalize_company(company)
    domain = normalize_domain(data.get("company_domain"))
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
        if not existing and domain:
            existing = conn.execute(
                """
                SELECT id FROM leads WHERE agent = ? AND company_domain = ?
                  AND (contact_name IS NULL OR contact_name = ? OR ? IS NOT NULL)
                ORDER BY
                  CASE WHEN contact_name IS NOT NULL AND TRIM(contact_name) != '' THEN 0 ELSE 1 END,
                  COALESCE(score, 0) DESC
                LIMIT 1
                """,
                (agent, domain, data.get("contact_name") or "", data.get("contact_name")),
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
            "contact_tier": data.get("contact_tier"),
            "email_inferred": data.get("email_inferred"),
            "email_verified": data.get("email_verified"),
            "email_source": data.get("email_source"),
            "company_domain": data.get("company_domain"),
            "linkedin_note": data.get("linkedin_note"),
            "reply_class": data.get("reply_class"),
            "revive_at": data.get("revive_at"),
            "priority_score": data.get("priority_score"),
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


def get_leads_by_statuses(
    statuses: list[str],
    agent: str = "woodway",
    limit: int = 10,
    *,
    require_contact: bool = False,
    require_identity: bool | None = None,
) -> list[dict]:
    init_db()
    # require_identity: named human is the gate (tier B/C OK without email)
    if require_identity is not None:
        require_contact = require_identity
    placeholders = ", ".join("?" * len(statuses))
    q = (
        f"SELECT * FROM leads WHERE agent = ? AND status IN ({placeholders})"
    )
    params: list = [agent, *statuses]
    if require_contact:
        q += " AND contact_name IS NOT NULL AND TRIM(contact_name) != ''"
    # Prefer highest priority first (signal-boosted), then oldest
    q += " ORDER BY COALESCE(priority_score, 0) DESC, created_at ASC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def skip_actava_company_placeholders(agent: str, kept_company_keys: set[str]) -> int:
    """Mark company-only rows not in digest keep-list as awaiting_contact (retry next run)."""
    if not kept_company_keys:
        return 0
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, company FROM leads
            WHERE agent = ? AND status IN ('imported', 'awaiting_contact')
              AND source IN ('actava', 'discover')
              AND (contact_name IS NULL OR TRIM(contact_name) = '')
            """,
            (agent,),
        ).fetchall()
        marked = 0
        for row in rows:
            key = normalize_company(row["company"] or "")
            if key and key not in kept_company_keys:
                conn.execute(
                    "UPDATE leads SET status = 'awaiting_contact', updated_at = ? WHERE id = ?",
                    (_now(), row["id"]),
                )
                marked += 1
        conn.commit()
    return marked


def mark_company_awaiting_contact(agent: str, company: str) -> bool:
    """Set company-only row to awaiting_contact when contact discovery finds no person."""
    init_db()
    key = normalize_company(company)
    now = _now()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM leads
            WHERE agent = ? AND company_normalized = ?
              AND (contact_name IS NULL OR TRIM(contact_name) = '')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (agent, key),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE leads SET status = 'awaiting_contact', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()
    return True


def get_awaiting_contact_companies(agent: str = "woodway", limit: int = 100) -> list[str]:
    """Companies that need contact discovery retry."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT company FROM leads
            WHERE agent = ? AND status = 'awaiting_contact'
              AND (contact_name IS NULL OR TRIM(contact_name) = '')
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    return [r["company"] for r in rows if r["company"]]


def get_companies_needing_contacts(agent: str = "woodway", limit: int = 50) -> list[str]:
    """
    Active companies still missing a usable outreach path:
    no named contact, or named contact with neither email nor LinkedIn.
    """
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT company FROM leads
            WHERE agent = ?
              AND status NOT IN ('skipped', 'emailed', 'replied')
              AND (
                contact_name IS NULL OR TRIM(contact_name) = ''
                OR (
                  (email IS NULL OR TRIM(email) = '')
                  AND (linkedin_url IS NULL OR TRIM(linkedin_url) = '')
                )
              )
            ORDER BY
              CASE WHEN status = 'awaiting_contact' THEN 0 ELSE 1 END,
              updated_at DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    # Dedupe preserving order
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        name = (r["company"] or "").strip()
        key = normalize_company(name)
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def merge_duplicate_domains(agent: str = "woodway") -> dict:
    """Merge leads sharing the same company_domain — keep richest row."""
    from .email_patterns import normalize_domain, resolve_company_domain

    init_db()
    merged = deleted = 0
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, company, company_domain, contact_name, email, score, status, updated_at
            FROM leads WHERE agent = ?
            """,
            (agent,),
        ).fetchall()

        by_domain: dict[str, list] = {}
        for row in rows:
            domain = normalize_domain(row["company_domain"]) or normalize_domain(
                resolve_company_domain(row["company"] or "")
            )
            if not domain:
                continue
            if domain not in by_domain:
                by_domain[domain] = []
            by_domain[domain].append(dict(row))

        for domain, group in by_domain.items():
            if len(group) < 2:
                continue

            def rank(r: dict) -> tuple:
                has_contact = 1 if (r.get("contact_name") or "").strip() else 0
                has_email = 1 if (r.get("email") or "").strip() else 0
                protected = 1 if r.get("status") in PROTECTED_STATUSES else 0
                return (protected, has_contact, has_email, r.get("score") or 0)

            group.sort(key=rank, reverse=True)
            keeper = group[0]
            keeper_id = keeper["id"]
            conn.execute(
                "UPDATE leads SET company_domain = ?, updated_at = ? WHERE id = ?",
                (domain, _now(), keeper_id),
            )
            for dup in group[1:]:
                if dup["id"] == keeper_id:
                    continue
                if dup.get("status") in PROTECTED_STATUSES:
                    continue
                conn.execute("DELETE FROM leads WHERE id = ?", (dup["id"],))
                deleted += 1
                merged += 1
        conn.commit()

    # Migrate legacy skipped company-only rows → awaiting_contact
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE leads SET status = 'awaiting_contact', updated_at = ?
            WHERE agent = ? AND status = 'skipped'
              AND (contact_name IS NULL OR TRIM(contact_name) = '')
            """,
            (_now(), agent),
        )
        conn.commit()

    return {"merged": merged, "deleted": deleted}


def prune_extra_contacts_per_company(agent: str = "woodway", *, keep: int = 2) -> dict:
    """Keep the best N named contacts per company; skip extras (not emailed/replied)."""
    init_db()
    skipped = 0
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, company, contact_name, email, score, status
            FROM leads
            WHERE agent = ?
              AND contact_name IS NOT NULL AND TRIM(contact_name) != ''
              AND status NOT IN ('skipped')
            """,
            (agent,),
        ).fetchall()
        by_co: dict[str, list] = {}
        for r in rows:
            key = normalize_company(r["company"] or "")
            by_co.setdefault(key, []).append(dict(r))

        def rank(r: dict) -> tuple:
            protected = 1 if r.get("status") in ("emailed", "replied") else 0
            drafted = 1 if r.get("status") == "drafted" else 0
            has_email = 1 if (r.get("email") or "").strip() else 0
            return (protected, drafted, has_email, r.get("score") or 0)

        for group in by_co.values():
            if len(group) <= keep:
                continue
            group.sort(key=rank, reverse=True)
            for extra in group[keep:]:
                if extra.get("status") in ("emailed", "replied"):
                    continue
                conn.execute(
                    "UPDATE leads SET status = 'skipped', updated_at = ? WHERE id = ?",
                    (_now(), extra["id"]),
                )
                skipped += 1
        conn.commit()
    return {"skipped": skipped, "keep": keep}


def resolve_awaiting_contact_shells(agent: str = "woodway") -> dict:
    """
    When a named contact exists for a company, close company-only awaiting_contact shells
    so the funnel doesn't show perpetual 'No contact yet' next to real people.
    """
    init_db()
    resolved = 0
    with get_connection() as conn:
        shells = conn.execute(
            """
            SELECT id, company FROM leads
            WHERE agent = ? AND status IN ('awaiting_contact', 'imported', 'discovered')
              AND (contact_name IS NULL OR TRIM(contact_name) = '')
            """,
            (agent,),
        ).fetchall()
        for shell in shells:
            company = (shell["company"] or "").strip()
            if not company:
                continue
            key = normalize_company(company)
            # Any richer row for same normalized company?
            peers = conn.execute(
                """
                SELECT id, company, contact_name, email, status FROM leads
                WHERE agent = ? AND id != ?
                  AND status NOT IN ('skipped', 'awaiting_contact')
                  AND contact_name IS NOT NULL AND TRIM(contact_name) != ''
                """,
                (agent, shell["id"]),
            ).fetchall()
            has_contact = any(normalize_company(p["company"] or "") == key for p in peers)
            if not has_contact:
                continue
            conn.execute(
                "UPDATE leads SET status = 'skipped', updated_at = ?, "
                "signal = COALESCE(signal, '') || ' · superseded by named contact' "
                "WHERE id = ?",
                (_now(), shell["id"]),
            )
            resolved += 1
        conn.commit()
    return {"resolved": resolved}


def purge_excluded_leads(agent: str = "woodway") -> dict:
    """Mark negative-list / vendor rows as skipped so they leave the active funnel."""
    from .negative_list import is_excluded

    init_db()
    purged = 0
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, company, industry, signal, status FROM leads
            WHERE agent = ? AND status NOT IN ('emailed', 'replied', 'skipped')
            """,
            (agent,),
        ).fetchall()
        for row in rows:
            extra = f"{row['industry'] or ''} {row['signal'] or ''}"
            if is_excluded(row["company"] or "", agent=agent, extra_text=extra):
                conn.execute(
                    "UPDATE leads SET status = 'skipped', updated_at = ? WHERE id = ?",
                    (_now(), row["id"]),
                )
                purged += 1
        conn.commit()
    return {"purged": purged}


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
    "mail_provider",
    "outreach_subject", "outreach_body",
    "qualification_json",
    "contact_tier", "email_inferred", "email_verified", "email_source",
    "company_domain", "linkedin_note", "reply_class", "revive_at", "priority_score",
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


def get_cursor(agent: str, name: str) -> int:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM agent_cursors WHERE agent = ? AND name = ?",
            (agent, name),
        ).fetchone()
    return int(row["value"]) if row else 0


def set_cursor(agent: str, name: str, value: int):
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_cursors (agent, name, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent, name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (agent, name, int(value), _now()),
        )
        conn.commit()


def active_domains(agent: str = "woodway") -> set[str]:
    """Domains already in the funnel — for deterministic post-extract dedupe."""
    from .email_patterns import normalize_domain

    init_db()
    out: set[str] = set()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT company_domain, email FROM leads
            WHERE agent = ? AND status NOT IN ('skipped')
            """,
            (agent,),
        ).fetchall()
    for row in rows:
        d = normalize_domain(row["company_domain"])
        if d:
            out.add(d)
        email = (row["email"] or "").strip().lower()
        if "@" in email:
            host = normalize_domain(email.split("@")[-1])
            if host:
                out.add(host)
    return out


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
        qualified_ready = conn.execute(
            """
            SELECT COUNT(*) FROM leads
            WHERE agent = ? AND status IN ('qualified', 'drafted')
              AND score IS NOT NULL AND score >= 50
            """,
            (agent,),
        ).fetchone()[0]
        awaiting = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND status = 'awaiting_contact'",
            (agent,),
        ).fetchone()[0]
    by_status_map = {r["status"]: r["n"] for r in by_status}
    return {
        "total": total,
        "with_email": with_email,
        "linkedin_only": linkedin_only,
        "by_status": by_status_map,
        "qualified_ready": qualified_ready,
        "awaiting_contact": awaiting,
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


# ---------------------------------------------------------------- auth


def get_user_by_email(email: str) -> dict | None:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def create_user(email: str, password_hash: str) -> dict:
    init_db()
    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email.strip().lower(), password_hash, now),
        )
        user_id = cur.lastrowid
        conn.commit()
    user = get_user_by_id(user_id)
    assert user is not None
    return user


def touch_user_login(user_id: int):
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (_now(), user_id),
        )
        conn.commit()


def create_invite_row(email: str, otp_hash: str, expires_at: str) -> int:
    init_db()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO invites (email, otp_hash, expires_at, used, created_at) VALUES (?, ?, ?, 0, ?)",
            (email.strip().lower(), otp_hash, expires_at, _now()),
        )
        conn.commit()
    return cur.lastrowid


def find_valid_invite(email: str, otp_hash: str) -> dict | None:
    init_db()
    now = _now()
    with get_connection() as conn:
        row = conn.execute(
            """SELECT * FROM invites
               WHERE email = ? COLLATE NOCASE AND otp_hash = ? AND used = 0 AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (email.strip(), otp_hash, now),
        ).fetchone()
    return dict(row) if row else None


def mark_invite_used(invite_id: int):
    init_db()
    with get_connection() as conn:
        conn.execute("UPDATE invites SET used = 1 WHERE id = ?", (invite_id,))
        conn.commit()


def create_session_row(session_id: str, user_id: int, expires_at: str):
    init_db()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, expires_at, _now()),
        )
        conn.commit()


def get_session_row(session_id: str) -> dict | None:
    init_db()
    now = _now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND expires_at > ?",
            (session_id, now),
        ).fetchone()
    return dict(row) if row else None


def delete_session_row(session_id: str):
    init_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()


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


# ---------------------------------------------------------------- evidence / signals / outcomes / costs helpers


def save_evidence(
    entity_type: str,
    field: str,
    value: str,
    *,
    entity_id: int | None = None,
    company: str | None = None,
    source_url: str | None = None,
    snippet: str | None = None,
) -> int | None:
    if not value or not str(value).strip():
        return None
    if not source_url or not snippet:
        return None
    init_db()
    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO evidence (entity_type, entity_id, company, field, value, source_url, snippet, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, company, field, str(value), source_url, (snippet or "")[:500], now),
        )
        conn.commit()
        return cur.lastrowid


def get_evidence(*, entity_type: str | None = None, entity_id: int | None = None, company: str | None = None) -> list[dict]:
    init_db()
    q = "SELECT * FROM evidence WHERE 1=1"
    params: list = []
    if entity_type:
        q += " AND entity_type = ?"
        params.append(entity_type)
    if entity_id is not None:
        q += " AND entity_id = ?"
        params.append(entity_id)
    if company:
        q += " AND company = ?"
        params.append(company)
    q += " ORDER BY extracted_at DESC"
    with get_connection() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def save_signal(data: dict, agent: str = "woodway") -> int:
    init_db()
    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                agent, company, company_domain, signal_type, strength, detected_at,
                half_life_days, source_url, snippet, lead_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent,
                data.get("company"),
                data.get("company_domain"),
                data["signal_type"],
                data.get("strength", 1.0),
                data.get("detected_at", now),
                data.get("half_life_days", 90),
                data.get("source_url"),
                (data.get("snippet") or "")[:500],
                data.get("lead_id"),
            ),
        )
        conn.commit()
        return cur.lastrowid


def list_signals(
    agent: str = "woodway",
    *,
    days: int = 7,
    unconsumed_only: bool = True,
    limit: int = 50,
    company: str | None = None,
) -> list[dict]:
    init_db()
    q = "SELECT * FROM signals WHERE agent = ?"
    params: list = [agent]
    if unconsumed_only:
        q += " AND consumed_at IS NULL"
    if company:
        q += " AND company = ?"
        params.append(company)
    if days:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        q += " AND detected_at >= ?"
        params.append(cutoff_iso)
    q += " ORDER BY strength DESC, detected_at DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def mark_signal_consumed(signal_id: int, *, lead_id: int | None = None) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE signals SET consumed_at = ?, lead_id = COALESCE(?, lead_id) WHERE id = ?",
            (_now(), lead_id, signal_id),
        )
        conn.commit()


def record_outcome(lead_id: int, event: str, *, reply_class: str | None = None, revive_at: str | None = None, notes: str | None = None):
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO outcomes (lead_id, event, reply_class, revive_at, occurred_at, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (lead_id, event, reply_class, revive_at, _now(), notes),
        )
        conn.commit()


def record_run_cost(run_id: str, step: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float):
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO run_costs (run_id, step, model, input_tokens, output_tokens, cost_usd, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, step, model, input_tokens, output_tokens, cost_usd, _now()),
        )
        conn.commit()


def run_cost_summary(run_id: str | None = None) -> dict:
    init_db()
    with get_connection() as conn:
        if run_id:
            rows = conn.execute(
                "SELECT SUM(cost_usd) as total, SUM(input_tokens) as inp, SUM(output_tokens) as out FROM run_costs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        else:
            rows = conn.execute(
                "SELECT SUM(cost_usd) as total, SUM(input_tokens) as inp, SUM(output_tokens) as out FROM run_costs",
            ).fetchone()
    return {
        "cost_usd": float(rows["total"] or 0),
        "input_tokens": int(rows["inp"] or 0),
        "output_tokens": int(rows["out"] or 0),
    }


def save_checkpoint(agent: str, run_id: str, step: str, summary: dict):
    import json

    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_checkpoints (agent, run_id, step, summary_json, completed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent, run_id, step) DO UPDATE SET
              summary_json = excluded.summary_json,
              completed_at = excluded.completed_at
            """,
            (agent, run_id, step, json.dumps(summary), _now()),
        )
        conn.commit()


def get_checkpoint(agent: str, run_id: str, step: str) -> dict | None:
    import json

    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT summary_json FROM pipeline_checkpoints WHERE agent = ? AND run_id = ? AND step = ?",
            (agent, run_id, step),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["summary_json"])
    except Exception:
        return None


def is_suppressed(*, email: str | None = None, domain: str | None = None, agent: str = "woodway") -> str | None:
    if os.getenv("SUPPRESSION_ENABLED", "true").lower() in ("0", "false", "no"):
        return None
    init_db()
    email = (email or "").strip().lower()
    domain = (domain or "").strip().lower()
    with get_connection() as conn:
        if email:
            row = conn.execute(
                "SELECT reason FROM suppression WHERE agent = ? AND (identifier = ? OR (scope = 'email' AND identifier = ?))",
                (agent, email, email),
            ).fetchone()
            if row:
                return row["reason"] or "suppressed"
        if domain:
            row = conn.execute(
                "SELECT reason FROM suppression WHERE agent = ? AND ((scope = 'domain' AND identifier = ?) OR domain = ?)",
                (agent, domain, domain),
            ).fetchone()
            if row:
                return row["reason"] or "suppressed"
    return None


def add_suppression(identifier: str, *, scope: str = "email", reason: str = "", agent: str = "woodway", domain: str | None = None):
    init_db()
    ident = identifier.strip().lower()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO suppression (agent, identifier, scope, domain, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (agent, ident, scope, domain, reason, _now()),
        )
        conn.commit()


def list_suppression(agent: str = "woodway", limit: int = 200) -> list[dict]:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM suppression WHERE agent = ? ORDER BY created_at DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def today_stats(agent: str = "woodway") -> dict:
    init_db()
    now = _now()
    today = now[:10]
    with get_connection() as conn:
        drafts = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND status = 'drafted'",
            (agent,),
        ).fetchone()[0]
        replies = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND status = 'replied' AND reply_class IN ('positive', 'referral')",
            (agent,),
        ).fetchone()[0]
        pending_replies = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND status = 'replied' AND (reply_class IS NULL OR reply_class = '')",
            (agent,),
        ).fetchone()[0]
        signals = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE agent = ? AND consumed_at IS NULL AND detected_at >= ?",
            (agent, datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()),
        ).fetchone()[0]
        reviving = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND revive_at IS NOT NULL AND revive_at <= ? AND status != 'skipped'",
            (agent, now),
        ).fetchone()[0]
    costs = run_cost_summary()
    return {
        "drafts_to_review": drafts,
        "positive_replies": replies,
        "replies_need_review": pending_replies,
        "new_signals": signals,
        "reviving_today": reviving,
        "total_cost_usd": costs["cost_usd"],
    }


def get_review_queue(agent: str = "woodway", limit: int = 50) -> list[dict]:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM leads
            WHERE agent = ? AND status IN ('drafted', 'qualified', 'replied')
              AND (status = 'drafted' OR (status = 'replied' AND (reply_class IS NULL OR reply_class IN ('positive', 'referral'))))
            ORDER BY COALESCE(priority_score, score, 0) DESC, updated_at DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    return [dict(r) for r in rows]
