"""Tests for reliability improvements."""

import pytest

from src.llm import LLMError


def test_qualify_llm_fallback_on_error(monkeypatch):
    from src.llm import LLMError
    from src.qualify import qualify_prospect

    config = {
        "name": "woodway",
        "company": "Woodway",
        "product": "EviData",
        "tagline": "Privacy",
        "icp": {"industries": ["pharma"], "titles": ["VP Data Governance"], "company_size": "large"},
        "disqualifiers": [],
    }

    def boom(*_a, **_k):
        raise LLMError("rate limit")

    monkeypatch.setattr("src.qualify._qualify_with_llm", boom)

    result = qualify_prospect(config, "Jane Doe, VP Data Governance at Big Pharma Inc")
    assert result["mode"] == "fallback"
    assert any("fallback" in r.lower() for r in result["reasons"])


def test_pipeline_outreach_fallback_on_llm_error(monkeypatch, tmp_db):
    from src.pipeline import process_lead

    config = {
        "name": "woodway",
        "company": "Woodway",
        "product": "EviData",
        "tagline": "Privacy",
        "icp": {"industries": ["pharma"], "titles": ["VP"], "company_size": "large"},
        "disqualifiers": [],
        "value_props": ["Risk metrics"],
    }

    monkeypatch.setattr(
        "src.pipeline.qualify_prospect",
        lambda *_a, **_k: {
            "score": 80, "tier": "hot", "reasons": [], "talking_points": ["angle"],
            "recommendation": "Reach out", "mode": "groq",
        },
    )
    monkeypatch.setattr(
        "src.pipeline.research_prospect",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "src.pipeline.enrich_company",
        lambda *_a, **_k: {},
    )

    def fail_llm(*_a, use_llm=True, **_k):
        from src.llm import LLMError

        if use_llm:
            raise LLMError("outreach failed")
        return {"subject": "Hi", "body": "Subject: Hi\n\nHello", "mode": "mock"}

    monkeypatch.setattr("src.pipeline.generate_outreach", fail_llm)

    result = process_lead(
        config,
        {"company": "Acme", "contact_name": "Jane", "source": "imported", "status": "imported"},
        agent_name="woodway",
        save=False,
    )
    assert result["outreach"]["mode"] == "fallback"


def test_process_imported_continues_on_lead_failure(monkeypatch, tmp_db):
    from src.agent import run_process_imported

    tmp_db.upsert_lead(
        {"company": "Good Co", "contact_name": "Alice", "source": "pdl", "status": "imported"},
        agent="woodway",
    )
    tmp_db.upsert_lead(
        {"company": "Bad Co", "contact_name": "Bob", "source": "pdl", "status": "imported"},
        agent="woodway",
    )

    calls = []

    def fake_process(config, lead, **_kw):
        calls.append(lead["company"])
        if lead["company"] == "Bad Co":
            raise RuntimeError("boom")
        return {
            "qualification": {"score": 70, "tier": "warm"},
            "outreach": None,
            "lead_id": lead.get("id"),
        }

    monkeypatch.setattr("src.pipeline.process_lead", fake_process)
    monkeypatch.setattr("src.agent.PROCESS_DELAY", 0)

    result = run_process_imported("woodway", limit=10, use_llm=False)
    assert result["count"] == 1
    assert result["failed_count"] == 1
    assert calls == ["Good Co", "Bad Co"]


def test_upsert_preserves_emailed_status(tmp_db):
    lead_id, _ = tmp_db.upsert_lead(
        {"company": "Sent Co", "email": "a@sent.com", "status": "emailed", "source": "pdl"},
        agent="woodway",
    )
    tmp_db.upsert_lead(
        {
            "id": lead_id,
            "company": "Sent Co",
            "email": "a@sent.com",
            "status": "imported",
            "score": 90,
            "source": "pdl",
        },
        agent="woodway",
    )
    row = tmp_db.get_lead(lead_id)
    assert row["status"] == "emailed"


def test_validate_config_with_keys(monkeypatch):
    from src.config_check import validate_config

    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setenv("PDL_API_KEY", "test")
    monkeypatch.setenv("CONTACTS_PROVIDER", "pdl")
    monkeypatch.setenv("LLM_CALL_DELAY", "2.5")

    monkeypatch.setattr(
        "src.config_check.check_llm",
        lambda: {"ok": True, "provider": "groq", "model": "x", "detail": None},
    )
    monkeypatch.setattr(
        "src.config_check.contacts_status",
        lambda: {"provider": "pdl", "configured": True, "email_finder": False},
    )
    monkeypatch.setattr(
        "src.config_check.gmail_status",
        lambda: {"connected": False, "email": None, "detail": "missing"},
    )

    cfg = validate_config()
    assert cfg["ok"] is True
    assert any("Gmail" in w for w in cfg["warnings"])
