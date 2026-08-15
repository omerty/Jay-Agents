"""Keira Phase 3 — eval harness, research memory, event rediscovery."""

from datetime import datetime, timedelta, timezone

from src.keira_eval import expand_synthetic_cases, format_report, load_eval_set, run_eval
from src.keira_events import detect_events, rescore_company, run_keira_rediscovery
from src.keira_memory import (
    fingerprint_lead,
    get_memory,
    remember_research,
    should_skip_research,
    status_from_qualification,
)


def test_eval_set_has_holdout_cases():
    cases = load_eval_set("keira")
    assert len(cases) >= 20
    expects = {c["expect"] for c in cases}
    assert "qualify" in expects and "reject" in expects


def test_synthetic_expansion_over_100():
    syn = expand_synthetic_cases()
    assert len(syn) >= 100


def test_keira_eval_passes_floors():
    summary = run_eval(include_synthetic=True)
    assert summary["metrics"]["n_total"] >= 120
    assert summary["pass"], format_report(summary)


def test_keira_eval_holdout_only():
    summary = run_eval(holdout_only=True)
    assert summary["metrics"]["n_holdout"] >= 20
    assert summary["metrics"]["overall_qualification"] >= 0.7


def test_memory_cooldown_and_fingerprint(tmp_db):
    remember_research(
        "ABC Manufacturing",
        status="research_later",
        reason="weak succession",
        fingerprint="aaa",
        cooldown_days=90,
    )
    mem = get_memory("ABC Manufacturing")
    assert mem and mem["status"] == "research_later"

    skip = should_skip_research("ABC Manufacturing", fingerprint="aaa")
    assert skip["skip"] is True

    # New evidence → allow research
    skip2 = should_skip_research("ABC Manufacturing", fingerprint="bbb")
    assert skip2["skip"] is False
    assert skip2["reason"] == "fingerprint_changed"


def test_memory_do_not_research(tmp_db):
    remember_research("Skip Co", status="do_not_research", reason="PE-backed")
    assert should_skip_research("Skip Co")["skip"] is True


def test_memory_due_after_cooldown(tmp_db):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    remember_research(
        "Due Co",
        status="research_later",
        reason="check again",
        force_next=past,
        fingerprint="x",
    )
    skip = should_skip_research("Due Co", fingerprint="x")
    assert skip["skip"] is False


def test_fingerprint_changes_with_signal():
    a = fingerprint_lead({"company": "X", "signal": "old"})
    b = fingerprint_lead({"company": "X", "signal": "new president appointed"})
    assert a != b


def test_status_from_qualification():
    st, _ = status_from_qualification({"rejected": True, "reject_reasons": ["geo"]})
    assert st == "rejected"
    st, _ = status_from_qualification(
        {"outreach_ok": True, "why_now": "retiring"},
        critic={"approved": True},
    )
    assert st == "qualified"


def test_detect_events():
    ev = detect_events("New president appointed; founder retiring")
    types = {e["type"] for e in ev}
    assert "new_president" in types
    assert "retirement" in types


def test_rescore_company_threshold():
    result = rescore_company({
        "company": "Ottawa Foundry",
        "contact_name": "Alex",
        "contact_title": "Founder & President",
        "company_city": "Ottawa",
        "employee_count": 70,
        "signal": "Founded in 1985. Succession planning underway. Next chapter.",
    })
    assert result["qual"]["score"] >= 50
    assert result["events"]
    assert result["crossed_threshold"]


def test_rediscovery_requeues(tmp_db, monkeypatch):
    monkeypatch.setenv("KEIRA_EVENT_REDISCOVERY", "true")
    monkeypatch.setenv("KEIRA_RESEARCH_MEMORY", "true")
    past = datetime.now(timezone.utc) - timedelta(days=1)
    remember_research(
        "Ottawa Event Co",
        status="research_later",
        reason="old",
        force_next=past,
        fingerprint="oldfp",
    )
    # Seed a lead the rediscovery sampler can also pick up
    from src.db import upsert_lead

    upsert_lead({
        "company": "Ottawa Event Co",
        "contact_name": "Sam Owner",
        "contact_title": "Founder & President",
        "employee_count": 60,
        "signal": "New president appointed; succession planning underway. Founded in 1984.",
        "source": "test",
        "status": "imported",
        "company_city": "Ottawa",
    }, agent="keira")

    # Patch companies_due path uses memory; also existing leads
    result = run_keira_rediscovery(limit=10)
    assert result["rescored"] >= 1
    assert result.get("ok")


def test_pipeline_respects_memory(tmp_db, monkeypatch):
    remember_research(
        "Ottawa Foundry Ltd",
        status="research_later",
        reason="already looked",
        fingerprint=fingerprint_lead({
            "company": "Ottawa Foundry Ltd",
            "contact_name": "Alex Owner",
            "contact_title": "Founder & President",
            "employee_count": 60,
            "signal": "Founded in 1985. Succession planning underway.",
        }),
        cooldown_days=90,
    )
    candidates = [
        {
            "company": "Ottawa Foundry Ltd",
            "contact_name": "Alex Owner",
            "contact_title": "Founder & President",
            "company_city": "Ottawa",
            "employee_count": 60,
            "signal": "Founded in 1985. Succession planning underway.",
            "source": "test",
        },
        {
            "company": "Kingston New Co",
            "contact_name": "Pat New",
            "contact_title": "Owner",
            "company_city": "Kingston",
            "employee_count": 50,
            "signal": "Succession planning and next chapter. Founded in 1988.",
            "source": "test",
        },
    ]
    monkeypatch.setattr("src.seamless_api.seamless_available", lambda: False)
    monkeypatch.setattr("src.actava_api.actava_available", lambda: False)
    monkeypatch.setattr(
        "src.discover.search_and_import_companies",
        lambda *a, **k: {"companies": candidates, "provider": "test"},
    )
    monkeypatch.setattr(
        "src.agent.run_process_imported",
        lambda *a, **k: {"count": 0, "processed": []},
    )
    monkeypatch.setenv("KEIRA_CLAUDE_RESEARCH", "false")
    monkeypatch.setenv("KEIRA_CLAUDE_CRITIC", "false")
    monkeypatch.setenv("KEIRA_EVENT_REDISCOVERY", "false")

    from src.keira_pipeline import run_keira_pipeline

    result = run_keira_pipeline(limit=5, use_llm=False, create_drafts=False)
    assert result["steps"]["gates"]["memory_skipped"] >= 1
    assert "rediscovery" in result["steps"]
