import pytest


def test_normalize_company(tmp_db):
    assert tmp_db.normalize_company("Acme Inc.") == "acme"
    assert tmp_db.normalize_company("Acme  Corp") == "acme"
    assert tmp_db.normalize_company("Foo-Bar Ltd") == "foo bar"


def test_upsert_insert_then_update(tmp_db):
    lead = {"company": "Acme Inc", "email": "a@acme.com", "source": "pdl_api", "status": "imported"}
    lead_id, created = tmp_db.upsert_lead(lead, agent="woodway")
    assert created

    lead["score"] = 80
    lead_id2, created2 = tmp_db.upsert_lead(lead, agent="woodway")
    assert not created2
    assert lead_id2 == lead_id

    rows = tmp_db.get_leads(agent="woodway")
    assert len(rows) == 1
    assert rows[0]["score"] == 80


def test_upsert_same_company_different_agent(tmp_db):
    lead = {"company": "Acme", "email": "a@acme.com", "source": "pdl_api"}
    tmp_db.upsert_lead(lead, agent="woodway")
    tmp_db.upsert_lead(lead, agent="fonex")
    assert len(tmp_db.get_leads(agent="woodway")) == 1
    assert len(tmp_db.get_leads(agent="fonex")) == 1


def test_upsert_by_id_prevents_reprocess_duplicates(tmp_db):
    """Re-processing a DB lead (carries its id) must update, not insert."""
    lead_id, _ = tmp_db.upsert_lead(
        {"company": "Acme", "contact_name": "Jane Doe", "source": "apollo", "status": "imported"},
        agent="woodway",
    )
    row = tmp_db.get_lead(lead_id)
    row["score"] = 77
    row["status"] = "qualified"
    lead_id2, created = tmp_db.upsert_lead(row, agent="woodway")
    assert not created
    assert lead_id2 == lead_id
    assert len(tmp_db.get_leads(agent="woodway")) == 1


def test_upsert_matches_no_email_contact_by_name(tmp_db):
    tmp_db.upsert_lead(
        {"company": "Acme", "contact_name": "Jane Doe", "source": "apollo"},
        agent="woodway",
    )
    _, created = tmp_db.upsert_lead(
        {"company": "Acme Inc.", "contact_name": "Jane Doe", "source": "apollo"},
        agent="woodway",
    )
    assert not created
    assert len(tmp_db.get_leads(agent="woodway")) == 1


def test_company_seen_ignores_skipped(tmp_db):
    lead_id, _ = tmp_db.upsert_lead({"company": "Acme", "source": "discover"}, agent="woodway")
    assert tmp_db.company_seen("Acme Inc.", agent="woodway")
    tmp_db.update_lead_status(lead_id, "skipped")
    assert not tmp_db.company_seen("Acme", agent="woodway")


def test_update_lead_status_rejects_invalid(tmp_db):
    lead_id, _ = tmp_db.upsert_lead({"company": "Acme", "source": "discover"}, agent="woodway")
    with pytest.raises(ValueError):
        tmp_db.update_lead_status(lead_id, "bogus")


def test_get_leads_limit(tmp_db):
    for i in range(5):
        tmp_db.upsert_lead(
            {"company": f"Co{i}", "email": f"x{i}@co{i}.com", "source": "csv", "score": i},
            agent="woodway",
        )
    rows = tmp_db.get_leads(agent="woodway", limit=2)
    assert len(rows) == 2
    assert rows[0]["score"] == 4  # ordered by score desc


def test_stats_and_export(tmp_db, tmp_path):
    tmp_db.upsert_lead(
        {"company": "Acme", "email": "a@acme.com", "source": "csv", "status": "imported"},
        agent="woodway",
    )
    s = tmp_db.stats(agent="woodway")
    assert s["total"] == 1
    assert s["with_email"] == 1
    assert s["by_status"] == {"imported": 1}

    out = tmp_path / "out.csv"
    assert tmp_db.export_csv(out, agent="woodway") == 1
    assert "a@acme.com" in out.read_text()


def test_build_prospect_string(tmp_db):
    assert tmp_db.build_prospect_string(
        {"contact_name": "Jane Doe", "contact_title": "CPO", "company": "Acme"}
    ) == "Jane Doe, CPO at Acme"
    assert tmp_db.build_prospect_string(
        {"contact_title": "CPO", "company": "Acme"}
    ) == "CPO at Acme"
