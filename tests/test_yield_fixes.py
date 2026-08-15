"""Tests for contact yield / dedupe / scoring fixes."""

from src.agent import load_agent
from src.db import normalize_company
from src.hunter_contacts import pick_contacts_from_domain_data
from src.negative_list import is_excluded
from src.normalize import normalize_display_name
from src.qualify import qualify_prospect


def test_normalize_company_aliases():
    assert normalize_company("Citigroup Inc") == normalize_company("Citi")


def test_normalize_display_name_lowercase():
    assert normalize_display_name("pfizer", kind="company") == "Pfizer"
    assert normalize_display_name("sofi", kind="company") == "SoFi"
    assert normalize_display_name("lalith somu", kind="person") == "Lalith Somu"


def test_negative_list_blocks_vendors():
    assert is_excluded("Epic Systems") == "blocklist"
    assert is_excluded("Carrier") == "blocklist"
    assert is_excluded("Carrier Global") == "blocklist"
    assert is_excluded("Axtria") == "blocklist"
    assert is_excluded("Axtria, Inc.") == "blocklist"
    assert is_excluded("ERNI") == "blocklist"


def test_hunter_pick_privacy_title():
    data = {
        "emails": [
            {"value": "jane.doe@acme.com", "first_name": "Jane", "last_name": "Doe", "position": "Intern"},
            {"value": "cpo@acme.com", "first_name": "Sam", "last_name": "Lee", "position": "Chief Privacy Officer"},
        ]
    }
    picks = pick_contacts_from_domain_data(data, target_titles=["Chief Privacy Officer"])
    assert picks[0]["contact_name"] == "Sam Lee"
    assert picks[0]["email"] == "cpo@acme.com"


def test_hunter_pick_privacy_alias():
    data = {
        "emails": [
            {"value": "privacy@pfizer.com", "first_name": None, "last_name": None, "position": None},
        ]
    }
    picks = pick_contacts_from_domain_data(data, target_titles=["Chief Privacy Officer"])
    assert len(picks) == 1
    assert picks[0]["email"] == "privacy@pfizer.com"


def test_upsert_dedupes_by_domain(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LEADS_DB_PATH", str(db_path))
    import importlib
    import src.db as db

    importlib.reload(db)
    db.init_db()
    id1, created1 = db.upsert_lead(
        {"company": "Citi", "source": "discover", "status": "imported", "company_domain": "citi.com"},
        agent="woodway",
    )
    id2, created2 = db.upsert_lead({
        "company": "Citigroup",
        "source": "discover",
        "status": "imported",
        "company_domain": "citi.com",
        "contact_name": "Jane Doe",
        "email": "jane@citi.com",
    }, agent="woodway")
    assert created1 is True
    assert created2 is False
    assert id1 == id2
    with db.get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM leads WHERE agent = 'woodway'").fetchone()[0]
    assert n == 1


def test_company_seen_by_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "seen.db"))
    import importlib
    import src.db as db

    importlib.reload(db)
    db.init_db()
    db.upsert_lead({"company": "Pfizer", "source": "discover", "status": "imported", "company_domain": "pfizer.com"}, agent="woodway")
    assert db.company_seen("Pfizer Inc", "woodway", domain="pfizer.com")


def test_purge_excluded_leads(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "purge.db"))
    import importlib
    import src.db as db

    importlib.reload(db)
    db.init_db()
    db.upsert_lead({"company": "Carrier", "source": "discover", "status": "imported"}, agent="woodway")
    db.upsert_lead({"company": "Pfizer", "source": "discover", "status": "imported"}, agent="woodway")
    result = db.purge_excluded_leads("woodway")
    assert result["purged"] >= 1
    leads = {r["company"]: r["status"] for r in db.get_leads(agent="woodway")}
    assert leads["Carrier"] == "skipped"
    assert leads["Pfizer"] == "imported"


def test_score_calibration_caps_without_industry(monkeypatch):
    config = load_agent("woodway")

    def fake_chat_json(prompt, system=None, **kwargs):
        return {
            "score": 92,
            "tier": "hot",
            "industries": [],
            "reasons": ["Generic fit"],
            "talking_points": [],
            "recommendation": "Reach out",
        }

    monkeypatch.setattr("src.llm.chat_json", fake_chat_json)
    monkeypatch.setenv("QUALIFY_TWO_PASS", "false")
    q = qualify_prospect(config, "Random Person, Analyst at UnknownCo", use_llm=True, company="UnknownCo")
    assert q["score"] <= 55
    assert q["tier"] == "warm"
