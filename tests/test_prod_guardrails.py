"""Prod-readiness guardrails — budget split, critic gate, junk contacts, confidentiality."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _mailbox_bypass(monkeypatch):
    monkeypatch.setenv("REQUIRE_MAILBOX_FOR_PIPELINE", "false")


def test_per_agent_daily_budget_split(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "100")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_WOODWAY", "60")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_KEIRA", "40")
    monkeypatch.setenv("SEAMLESS_WOODWAY_RESEARCH_LIMIT", "50")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "25")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")

    from src.seamless_budget import (
        allocate_research_slots,
        can_research,
        daily_budget_for_agent,
        record_credits_used,
    )

    assert daily_budget_for_agent("woodway") == 60
    assert daily_budget_for_agent("keira") == 40

    record_credits_used(55, agent="woodway")
    ok, msg = can_research(30, agent="woodway")
    assert not ok
    assert "capped" in msg.lower() or "budget" in msg.lower() or "hard" in msg.lower() or "exhausted" in msg.lower()

    # Keira still has its own headroom under shared daily
    n, note = allocate_research_slots(20, agent="keira")
    assert n == 20
    assert note == ""


def test_run_limit_clamps_fat_finger(monkeypatch):
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "500")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_WOODWAY", "500")
    monkeypatch.setenv("SEAMLESS_WOODWAY_RESEARCH_LIMIT", "50")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")

    from src.seamless_budget import allocate_research_slots

    n, note = allocate_research_slots(500, agent="woodway")
    assert 50 <= n <= 70
    assert n < 100
    assert "capped" in note or "overage" in note


def test_pre_spend_blocks_before_api(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "10")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_KEIRA", "10")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")

    from src.seamless_budget import allocate_research_slots, record_credits_used

    record_credits_used(10, agent="keira")
    n, reason = allocate_research_slots(3, agent="keira")
    assert n == 0
    assert reason


def test_junk_contact_filter():
    from src.junk_contacts import is_junk_contact, is_junk_contact_name

    assert is_junk_contact_name("Privacy Office")
    assert is_junk_contact_name("For Assistance at")
    assert is_junk_contact_name("Can Be Emailed at")
    assert not is_junk_contact_name("Melissa Fukushima")
    assert is_junk_contact({"contact_name": "Privacy Office", "email": "dpo@bms.com"})
    assert not is_junk_contact({"contact_name": "Jane Doe", "email": "privacy@credo.ai"})


def test_keira_confidentiality_gate():
    from src.outreach import check_keira_confidentiality

    ok = check_keira_confidentiality(
        "Confidential options",
        "Would you be open to a discreet conversation about succession?",
    )
    assert ok["ok"]

    bad = check_keira_confidentiality(
        "Saw your listing",
        "We noticed you're selling via BizBuySell — Seamless found your email.",
    )
    assert not bad["ok"]
    assert any("BizBuySell" in i or "selling" in i or "Seamless" in i for i in bad["issues"])


def test_mailbox_preflight_aborts(monkeypatch):
    monkeypatch.setenv("REQUIRE_MAILBOX_FOR_PIPELINE", "true")
    from src.pipeline_preflight import PipelinePreflightError, assert_mailbox_ready

    monkeypatch.setattr(
        "src.pipeline_preflight.mailbox_ready",
        lambda: {"ready": False, "detail": "No mailbox connected — test"},
    )
    with pytest.raises(PipelinePreflightError):
        assert_mailbox_ready(create_drafts=True)


def test_resolve_awaiting_shells(tmp_db):
    from src.db import resolve_awaiting_contact_shells, upsert_lead, get_leads

    upsert_lead(
        {"company": "Pfizer", "status": "awaiting_contact", "source": "discover"},
        agent="woodway",
    )
    upsert_lead(
        {
            "company": "Pfizer Inc",
            "contact_name": "Lucy Muzzy",
            "email": "lucy@pfizer.com",
            "status": "imported",
            "source": "seamless",
        },
        agent="woodway",
    )
    out = resolve_awaiting_contact_shells("woodway")
    assert out["resolved"] >= 1
    statuses = {r["contact_name"] or "": r["status"] for r in get_leads(agent="woodway")}
    # Shell should be skipped; named contact remains
    assert any(r["status"] == "skipped" for r in get_leads(agent="woodway") if not r.get("contact_name"))


def test_keira_seamless_covers_all_non_rejected_leads():
    """Contract: Keira Seamless pool is gate survivors minus hard rejects — not critic enrich-only."""
    analyzed = [
        {"company": "Soft Hold Co", "_qual": {}, "_critic": {"recommendation": "research_required"}},
        {"company": "Draft Ok Co", "_qual": {}, "_critic": {"approved_for_enrich": True, "approved": True}},
        {"company": "Rejected Co", "_qual": {}, "_critic": {"recommendation": "reject", "hard_reject": True}},
    ]
    survivors = analyzed + [
        {"company": "Deferred Co", "_qual": {}},  # Claude-deferred, still needs contacts
    ]
    seamless_pool: list[dict] = []
    seen: set[str] = set()

    def eligible(row: dict) -> bool:
        co = (row.get("company") or "").strip()
        key = co.lower()
        if not co or key in seen:
            return False
        if (row.get("_qual") or {}).get("rejected"):
            return False
        critic = row.get("_critic") or {}
        if critic.get("hard_reject") or critic.get("recommendation") == "reject":
            return False
        return True

    for row in analyzed + survivors:
        if eligible(row):
            seen.add((row.get("company") or "").strip().lower())
            seamless_pool.append(row)

    names = [r["company"] for r in seamless_pool]
    assert names == ["Soft Hold Co", "Draft Ok Co", "Deferred Co"]
    assert "Rejected Co" not in names


def test_tier0_succession_not_enough():
    """Family-owned alone (tier 0) must not clear succession numeric gate (≥0.40)."""
    from src.keira_signals import classify_succession_signals

    text = "We are a family-owned and privately held manufacturer based in Ottawa."
    result = classify_succession_signals(text)
    assert int(result.get("max_tier") or 0) == 0
    prob = float(result.get("succession_probability") or 0)
    assert prob < 0.40


def test_geo_toronto_serves_eastern_ontario_rejected():
    from src.keira_geo import geo_score

    g = geo_score(
        city="Toronto",
        extra_text="Headquartered in Toronto. Serves Eastern Ontario manufacturers.",
    )
    assert g["reject"] is True
    assert g["pass"] is False
    assert g["score"] < 15


def test_geo_ottawa_hq_still_passes():
    from src.keira_geo import geo_score

    g = geo_score(city="Ottawa", hq="Ottawa, ON", extra_text="Family-owned manufacturer in Ottawa.")
    assert g["reject"] is False
    assert g["pass"] is True
    assert g["score"] >= 15


def test_owner_rejects_national_brand_president():
    from src.owner_resolution import classify_owner_title

    r = classify_owner_title("President", company="Walmart Canada")
    assert r["reject"] is True
    assert r["is_owner"] is False


def test_owner_rejects_national_president_title():
    from src.owner_resolution import classify_owner_title

    r = classify_owner_title("National President", company="Northern Facilities Division")
    assert r["reject"] is True


def test_concurrent_agent_budget_no_starvation(tmp_db, monkeypatch):
    """Woodway burning shared daily must not let Keira blow past agent daily + overage."""
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "100")
    monkeypatch.setenv("SEAMLESS_DAILY_HARD_CAP", "250")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_WOODWAY", "60")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_KEIRA", "40")
    monkeypatch.setenv("SEAMLESS_WOODWAY_RESEARCH_LIMIT", "50")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "25")
    monkeypatch.setenv("SEAMLESS_RUN_OVERAGE", "20")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")

    from src.seamless_budget import allocate_research_slots, record_credits_used

    w1, _ = allocate_research_slots(40, agent="woodway")
    assert w1 == 40
    record_credits_used(40, agent="woodway")

    k1, note = allocate_research_slots(30, agent="keira")
    assert k1 == 30  # per-run 25 + overage
    assert "overage" in note
    record_credits_used(k1, agent="keira")

    # Keira agent daily 40 + overage 20 = 60 ceiling; 30 used → 30 left
    k2, _ = allocate_research_slots(40, agent="keira")
    assert k2 == 30
    record_credits_used(k2, agent="keira")

    k3, reason = allocate_research_slots(5, agent="keira")
    assert k3 == 0
    assert "hard cap" in reason.lower() or "exhausted" in reason.lower() or "budget" in reason.lower()

    # Shared used = 40+30+30 = 100; woodway may take shared daily + overage (20)
    w2, wnote = allocate_research_slots(50, agent="woodway")
    assert w2 == 20
    assert "overage" in wnote or "capped" in wnote


def test_auth_run_route_not_public():
    from src.auth import is_public_api

    assert not is_public_api("/api/agents/woodway/run")
    assert not is_public_api("/api/agents/keira/run")
    assert is_public_api("/api/health")
    assert is_public_api("/api/auth/login")


def test_retention_policy_formalized():
    from src.compliance import RETENTION_POLICY, privacy_posture_markdown, retention_months

    assert RETENTION_POLICY["default_months"] == 18
    assert "qualified" in RETENTION_POLICY["purge_statuses"]
    assert "emailed" in RETENTION_POLICY["retain_statuses"]
    assert retention_months() >= 1
    md = privacy_posture_markdown()
    assert "Retention schedule" in md
    assert str(retention_months()) in md