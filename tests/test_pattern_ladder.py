"""Tests for free email-pattern ladder (Europe PMC, SEC, inference, cache)."""

from src.email_patterns import (
    apply_pattern_to_name,
    infer_pattern_from_email,
)
from src.europe_pmc import _affiliation_emails
from src.sec_edgar import _personish, lookup_cik


def test_infer_pattern_first_dot_last():
    assert infer_pattern_from_email("jessica.fagan@pfizer.com", "Jessica", "Fagan") == "{first}.{last}"


def test_infer_pattern_flast():
    assert infer_pattern_from_email("jfagan@pfizer.com", "Jessica", "Fagan") == "{f}{last}"


def test_infer_pattern_skips_role_alias():
    assert infer_pattern_from_email("privacy@pfizer.com", None, None) is None


def test_apply_pattern():
    email = apply_pattern_to_name("{first}.{last}", "Jane", "Doe", "acme.com")
    assert email == "jane.doe@acme.com"


def test_pmc_affiliation_email_parse():
    result = {
        "title": "Example paper",
        "authorList": {
            "author": [{
                "firstName": "Jessica",
                "lastName": "Fagan",
                "authorAffiliationDetailsList": {
                    "authorAffiliation": [{
                        "affiliation": "Pfizer Inc. jessica.fagan@pfizer.com."
                    }]
                },
            }]
        },
    }
    rows = _affiliation_emails(result, prefer_domain="pfizer.com")
    assert rows[0]["email"] == "jessica.fagan@pfizer.com"
    assert rows[0]["first_name"] == "Jessica"


def test_sec_personish_filters_role_alias():
    assert _personish("kevin.coco@jpmorgan.com") is True
    assert _personish("corporate.secretary@jpmchase.com") is False


def test_sec_lookup_cik_jpmorgan(monkeypatch):
    fake = {
        "0": {"cik_str": 19617, "ticker": "JPM", "title": "JPMORGAN CHASE & CO"},
        "1": {"cik_str": 831001, "ticker": "C", "title": "CITIGROUP INC"},
    }
    monkeypatch.setattr("src.sec_edgar._tickers", lambda: fake)
    hit = lookup_cik("JPMorgan Chase")
    assert hit is not None
    cik, title = hit
    assert cik == "0000019617"
    assert "JPMORGAN" in title.upper()


def test_resolve_pattern_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "pat.db"))
    import importlib
    import src.db as db
    import src.email_patterns as ep

    importlib.reload(db)
    importlib.reload(ep)
    db.init_db()
    ep.save_pattern("pfizer.com", pattern="{first}.{last}", confidence=0.9, source="test", sample_email="a.b@pfizer.com")

    row = ep.resolve_pattern("Pfizer", domain="pfizer.com", allow_hunter=False)
    assert row["pattern"] == "{first}.{last}"
    assert row["source"] == "test"


def test_resolve_pattern_from_leads_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "pat2.db"))
    import importlib
    import src.db as db
    import src.email_patterns as ep

    importlib.reload(db)
    importlib.reload(ep)
    db.init_db()
    db.upsert_lead({
        "company": "Pfizer",
        "contact_name": "Jessica Fagan",
        "email": "jessica.fagan@pfizer.com",
        "company_domain": "pfizer.com",
        "source": "test",
        "status": "imported",
    }, agent="woodway")

    row = ep.resolve_pattern("Pfizer", domain="pfizer.com", allow_hunter=False)
    assert row is not None
    assert row["pattern"] == "{first}.{last}"
    assert row["source"] == "leads_db"
