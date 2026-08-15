"""Keira Phase 2 — evidence, budget, Claude analyst/critic."""

from datetime import datetime, timedelta, timezone

from src.keira_budget import allocate_budget, research_priority
from src.keira_evidence import (
    aggregate_evidence_confidence,
    decay_factor,
    evidence_object,
    source_weight,
    weight_signal,
)
from src.keira_intelligence import build_company_intelligence, qualify_keira_intelligence
from src.keira_research import (
    build_research_packet,
    merge_analyst_into_intel,
    run_lead_critic,
    run_succession_analyst,
)
from src.keira_signals import classify_succession_signals


def test_source_reliability_ordering():
    assert source_weight("company_website") > source_weight("seamless")
    assert source_weight("llm_inference") == 0.0
    assert source_weight("government_registry") == 1.0


def test_signal_decay_buckets():
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    recent = (now - timedelta(days=30)).isoformat()
    old = (now - timedelta(days=800)).isoformat()
    assert decay_factor(source_date=recent, now=now) == 1.0
    assert decay_factor(source_date=old, now=now) == 0.20


def test_weight_signal_applies_reliability_and_decay():
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    s = weight_signal(
        {
            "type": "retirement",
            "strength": 1.0,
            "confidence": 1.0,
            "source": "company_website",
            "source_date": (now - timedelta(days=400)).isoformat(),
        },
        now=now,
    )
    assert s["decay"] == 0.50
    assert s["effective_strength"] == 0.5
    assert s["source_reliability"] == 1.0


def test_evidence_object_structure():
    e = evidence_object(
        claim="Founder remains President",
        evidence="John founded ABC in 1987 and remains President",
        source="company_website",
        signal_type="founder_longevity",
        strength=0.9,
    )
    assert e["claim"] and e["observed_at"]
    assert e["effective_confidence"] > 0


def test_succession_signals_include_evidence_confidence():
    s = classify_succession_signals(
        "Succession planning underway; next chapter after 40 years"
    )
    assert "evidence_confidence" in s
    assert s["max_tier"] >= 2


def test_research_priority_separates_icp_timing_confidence():
    q = {
        "icp_fit": 90,
        "timing": 80,
        "evidence_confidence": 70,
        "outreach_ok": True,
        "rejected": False,
    }
    p = research_priority(q, {
        "geo": {"strong_city": "ottawa"},
        "ownership": {"founder": True, "title": "Founder"},
        "company": {"founded": 1987, "employees": 80},
        "succession": {"max_tier": 3},
    })
    assert p["lead_priority"] == "HIGH"
    assert p["research_priority"] >= 75
    assert "Ottawa HQ" in p["reason"] or any("Ottawa" in r for r in p["reason"])


def test_allocate_budget_orders_by_priority():
    cands = [
        {"company": "A", "score": 40, "_qual": {"icp_fit": 40, "timing": 20, "evidence_confidence": 20}},
        {"company": "B", "score": 90, "_qual": {"icp_fit": 90, "timing": 85, "evidence_confidence": 80, "outreach_ok": True}},
        {"company": "C", "score": 70, "_qual": {"icp_fit": 70, "timing": 60, "evidence_confidence": 50}},
    ]
    out = allocate_budget(cands, claude_cap=2, seamless_cap=2)
    assert len(out["claude_queue"]) == 2
    assert out["claude_queue"][0]["company"] == "B"


def test_research_packet_contains_sections():
    intel = build_company_intelligence({
        "company": "ABC Mfg",
        "contact_name": "John Smith",
        "contact_title": "Founder & President",
        "company_city": "Ottawa",
        "employee_count": 80,
        "signal": "Succession planning underway since founded in 1987",
    })
    packet = build_research_packet(intel)
    assert "COMPANY" in packet and "OWNERSHIP" in packet and "TASK" in packet


def test_analyst_heuristic_passthrough_without_llm():
    intel = build_company_intelligence({
        "company": "ABC",
        "contact_title": "Owner",
        "company_city": "Kingston",
        "employee_count": 40,
        "signal": "Family-owned",
    })
    result = run_succession_analyst(intel, use_llm=False)
    assert result["mode"] == "heuristic"


def test_merge_analyst_blends_probability():
    intel = build_company_intelligence({
        "company": "ABC",
        "contact_title": "Founder & President",
        "company_city": "Ottawa",
        "employee_count": 70,
        "signal": "Succession planning and next chapter; founded in 1988",
    })
    h = float(intel["succession"]["succession_probability"])
    merged = merge_analyst_into_intel(intel, {
        "succession_probability": 0.9,
        "signals": [{"type": "retirement", "tier": 3, "evidence": "retiring", "source": "news"}],
        "why_now": "Founder retiring",
        "mode": "claude",
    })
    blended = merged["succession"]["succession_probability"]
    assert abs(blended - (0.4 * h + 0.6 * 0.9)) < 0.02
    assert merged["analyst"]["why_now"] == "Founder retiring"


def test_critic_rejects_weak_succession_heuristic():
    """Tier-0 family-owned alone: no enrich, no draft."""
    intel = build_company_intelligence({
        "company": "Family Co",
        "contact_name": "Pat",
        "contact_title": "Owner",
        "company_city": "Ottawa",
        "employee_count": 50,
        "signal": "Family-owned business",
    })
    q = qualify_keira_intelligence(intel)
    critic = run_lead_critic(intel=intel, qualification=q, use_llm=False)
    assert critic["mode"] == "heuristic"
    assert not critic["approved"]
    assert not critic.get("approved_for_enrich")
    assert not critic.get("approved_for_draft")
    assert critic["recommendation"] in ("research_required", "reject")


def test_critic_soft_enrich_tier2_not_draft():
    """Owner+strong geo+tier2 → soft enrich; drafts still need tier3."""
    intel = build_company_intelligence({
        "company": "Ottawa Foundry Ltd",
        "contact_name": "Sam Owner",
        "contact_title": "Founder & President",
        "company_city": "Ottawa",
        "employee_count": 85,
        "signal": (
            "Founded in 1985. Daughter is VP Operations. "
            "Succession planning and next chapter on website."
        ),
    })
    q = qualify_keira_intelligence(intel)
    critic = run_lead_critic(
        intel=intel, qualification=q, use_llm=False,
        enrich_min_tier=2, draft_min_tier=3,
    )
    assert critic["approved_for_enrich"]
    assert critic["approved"]
    assert not critic["approved_for_draft"]
    assert critic["recommendation"] == "enrich"
    assert int(intel["succession"]["max_tier"]) >= 2
    assert int(intel["succession"]["max_tier"]) < 3


def test_critic_approves_strong_lead_heuristic():
    intel = build_company_intelligence({
        "company": "ABC Manufacturing Inc.",
        "contact_name": "John Smith",
        "contact_title": "Founder & President",
        "company_city": "Ottawa",
        "employee_count": 85,
        "signal": (
            "Founded in 1987. Daughter is VP Operations. "
            "Succession planning underway; founder retiring next year; next chapter."
        ),
        "industry": "Manufacturing",
    })
    q = qualify_keira_intelligence(intel)
    critic = run_lead_critic(
        intel=intel, qualification=q, use_llm=False,
        enrich_min_tier=2, draft_min_tier=3,
    )
    assert critic["approved"]
    assert critic["approved_for_enrich"]
    assert critic["approved_for_draft"]
    assert critic["recommendation"] == "enrich_and_draft"
    assert int(intel["succession"]["max_tier"]) >= 3


def test_pipeline_phase2_with_heuristic(tmp_db, monkeypatch):
    candidates = [
        {
            "company": "Ottawa Foundry Ltd",
            "contact_name": "Alex Owner",
            "contact_title": "Founder & President",
            "company_city": "Ottawa",
            "employee_count": 60,
            "signal": "Founded in 1985. Succession planning underway. Next chapter.",
            "source": "test",
        },
        {
            "company": "Toronto Giant Inc",
            "contact_name": "Bob Corp",
            "contact_title": "CEO",
            "company_city": "Toronto",
            "employee_count": 200,
            "signal": "Publicly traded manufacturer",
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

    from src.keira_pipeline import run_keira_pipeline

    result = run_keira_pipeline(limit=5, use_llm=False, create_drafts=False)
    assert "budget" in result["steps"]
    assert "analyst" in result["steps"]
    assert "critic" in result["steps"]
    assert result["steps"]["gates"]["rejected"] >= 1
    # Strong Ottawa lead should reach critic approved / enrich path
    assert result["steps"]["critic"]["approved"] >= 1


def test_aggregate_confidence_rewards_diverse_sources():
    low = aggregate_evidence_confidence([
        {"confidence": 0.7, "source": "directory", "strength": 0.5},
    ])
    high = aggregate_evidence_confidence([
        {"confidence": 0.8, "source": "company_website", "strength": 0.8},
        {"confidence": 0.8, "source": "news", "strength": 0.7},
    ])
    assert high > low
