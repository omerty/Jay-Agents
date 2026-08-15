"""Keira yield sprint — owner-shaped search, cheap email fill, overage, funnel."""

from __future__ import annotations

import yaml

from src.funnel_metrics import compute_funnel_from_summary
from src.owner_resolution import classify_owner_title, is_owner_like_title
from src.web_contacts import _company_contact_queries


def test_keira_web_queries_are_owner_shaped():
    qs = _company_contact_queries("Rideau Precision", ["Owner", "Founder"], agent="keira")
    blob = " ".join(qs).lower()
    assert "owner" in blob
    assert "founder" in blob
    assert "cpo" not in blob
    assert "data protection officer" not in blob
    assert "chief privacy" not in blob


def test_woodway_web_queries_still_privacy_shaped():
    qs = _company_contact_queries("Acme Pharma", ["Chief Privacy Officer"], agent="woodway")
    blob = " ".join(qs).lower()
    assert "privacy" in blob or "data governance" in blob


def test_owner_like_title_prefers_owner_over_vp():
    assert is_owner_like_title("Owner")
    assert is_owner_like_title("Founder & CEO")
    assert not is_owner_like_title("VP Sales")
    assert classify_owner_title("VP Sales")["reject"] is True


def test_vp_placeholder_does_not_block_later_owner(tmp_db):
    tmp_db.init_db()
    tmp_db.upsert_lead(
        {
            "company": "Rideau Precision",
            "contact_name": "Sam Patel",
            "contact_title": "VP Sales",
            "status": "imported",
            "source": "seamless",
        },
        agent="keira",
    )
    from src.seamless import _contact_seen

    assert _contact_seen(
        {
            "company": "Rideau Precision",
            "contact_name": "Margaret Chen",
            "contact_title": "Owner",
        },
        "keira",
    ) is False
    assert _contact_seen(
        {
            "company": "Rideau Precision",
            "contact_name": "Sam Patel",
            "contact_title": "VP Sales",
        },
        "keira",
    ) is True


def test_owner_contact_blocks_second_owner(tmp_db):
    tmp_db.init_db()
    tmp_db.upsert_lead(
        {
            "company": "Rideau Precision",
            "contact_name": "Margaret Chen",
            "contact_title": "Owner",
            "status": "imported",
            "source": "seamless",
        },
        agent="keira",
    )
    from src.seamless import _contact_seen

    assert _contact_seen(
        {
            "company": "Rideau Precision",
            "contact_name": "Other Person",
            "contact_title": "President",
        },
        "keira",
    ) is True


def test_cheap_fill_infers_email_without_critic(tmp_db, monkeypatch):
    tmp_db.init_db()
    tmp_db.upsert_lead(
        {
            "company": "Rideau Precision",
            "contact_name": "Margaret Chen",
            "contact_title": "Founder & President",
            "status": "imported",
            "source": "seamless",
            "company_domain": "rideauprecision.test",
        },
        agent="keira",
    )

    def fake_infer(name, company, domain=None):
        return {"email": "margaret.chen@rideauprecision.test", "email_source": "pattern"}

    monkeypatch.setattr("src.contact_enrich.infer_email_for_contact", fake_infer)
    monkeypatch.setattr("src.contact_enrich.resolve_company_domain", lambda *a, **k: "rideauprecision.test")
    monkeypatch.setattr("src.email_patterns.resolve_pattern", lambda *a, **k: None)
    monkeypatch.setattr("src.email_patterns.resolve_company_domain", lambda *a, **k: "rideauprecision.test")
    monkeypatch.setattr("src.site_emails.sweep_company_site", lambda *a, **k: [])

    from src.contact_enrich import enrich_imported_contacts

    result = enrich_imported_contacts("keira", ["Rideau Precision"])
    assert result["inferred_emails"] >= 1
    with tmp_db.get_connection() as conn:
        row = conn.execute(
            "SELECT email FROM leads WHERE company = ? AND agent = 'keira'",
            ("Rideau Precision",),
        ).fetchone()
    assert row["email"] == "margaret.chen@rideauprecision.test"


def test_keira_discovery_queries_cover_corridor_towns():
    from pathlib import Path

    cfg = yaml.safe_load((Path("agents/keira/config.yaml")).read_text())
    queries = (cfg.get("seamless") or {}).get("discovery_queries") or []
    names = {q.get("name") for q in queries}
    assert "corridor_towns" in names
    assert "kingston_owners" in names
    kingston = next(q for q in queries if q.get("name") == "kingston_owners")
    assert "industry" in (kingston.get("omit") or [])
    corridor = next(q for q in queries if q.get("name") == "corridor_towns")
    cities = [c.lower() for c in (corridor.get("company_city") or [])]
    assert "renfrew" in cities
    assert "gatineau" in cities


def test_keira_funnel_from_summary():
    metrics = compute_funnel_from_summary({
        "agent": "keira",
        "run_id": "abc123",
        "steps": {
            "discovery": {"candidates": 40},
            "gates": {"survivors": 8},
            "cheap_email": {"inferred_emails": 3},
            "critic": {"enrich_queue": 4},
            "contacts": {"with_email": 2},
            "process": {"count": 4},
            "drafts": {"created": 1},
        },
    })
    assert metrics["agent"] == "keira"
    assert metrics["discovered"] == 40
    assert metrics["kept"] == 8
    assert metrics["with_email"] == 5
    assert metrics["critic_enrich"] == 4
    assert metrics["drafted"] == 1
    assert metrics["rates"]["discover_to_keep"] == 0.2


def test_run_overage_allows_small_overshoot(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "40")
    monkeypatch.setenv("SEAMLESS_DAILY_HARD_CAP", "250")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_KEIRA", "40")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "5")
    monkeypatch.setenv("SEAMLESS_RUN_OVERAGE", "20")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")

    from src.seamless_budget import allocate_research_slots

    n, note = allocate_research_slots(12, agent="keira")
    assert n == 12
    assert "overage" in note


def test_daily_hard_cap_blocks_thousand_spend(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "40")
    monkeypatch.setenv("SEAMLESS_DAILY_HARD_CAP", "40")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_KEIRA", "40")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "25")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")

    from src.seamless_budget import allocate_research_slots, record_credits_used

    record_credits_used(40, agent="keira")
    n, reason = allocate_research_slots(20, agent="keira")
    assert n == 0
    assert "hard cap" in reason.lower() or "exhausted" in reason.lower()

    n2, _ = allocate_research_slots(1000, agent="keira")
    assert n2 == 0


def test_match_site_email_to_owner_name():
    from src.site_emails import match_email_to_name

    emails = ["info@fab.ca", "margaret.chen@fab.ca", "privacy@fab.ca"]
    assert match_email_to_name("Margaret Chen", emails) == "margaret.chen@fab.ca"
    assert match_email_to_name("Nobody Here", emails) is None
