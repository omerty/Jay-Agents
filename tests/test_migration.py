import json
import sqlite3


def test_migration_adds_qualification_json(tmp_path, monkeypatch):
    """Old databases (without qualification_json) must be upgraded in place."""
    from src import db

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL DEFAULT 'woodway',
            company TEXT NOT NULL,
            company_normalized TEXT NOT NULL,
            contact_name TEXT, contact_title TEXT, email TEXT, linkedin_url TEXT,
            industry TEXT, employee_count INTEGER, signal TEXT, prospect TEXT,
            score INTEGER, tier TEXT, outreach_subject TEXT, outreach_body TEXT,
            source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'discovered',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO leads (company, company_normalized, source, created_at, updated_at) "
        "VALUES ('Acme', 'acme', 'discover', '2025-01-01', '2025-01-01')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    db._initialized_paths.discard(str(path))
    db.init_db()

    # old row survives, new column exists and is writable
    rows = db.get_leads(agent="woodway")
    assert len(rows) == 1
    assert rows[0]["qualification_json"] is None

    db.upsert_lead(
        {
            "company": "Acme",
            "source": "discover",
            "qualification_json": json.dumps({"reasons": ["r1"]}),
        },
        agent="woodway",
    )
    rows = db.get_leads(agent="woodway")
    assert json.loads(rows[0]["qualification_json"]) == {"reasons": ["r1"]}
    db._initialized_paths.discard(str(path))
