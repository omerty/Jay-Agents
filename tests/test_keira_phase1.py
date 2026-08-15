"""Keira Phase 1 — geo, owner, ownership, succession, intelligence gates."""

from src.keira_geo import geo_score
from src.keira_ownership import classify_ownership
from src.keira_signals import classify_succession_signals
from src.keira_intelligence import (
    build_company_intelligence,
    qualify_keira_intelligence,
    build_mna_thesis,
)
from src.owner_resolution import classify_owner_title


def test_geo_ottawa_hq_passes():
    g = geo_score(hq="Ottawa", city="Ottawa", state="Ontario")
    assert g["pass"] and g["score"] >= 15
    assert g["strong_city"] == "ottawa"


def test_geo_ontario_only_fails():
    g = geo_score(extra_text="Based in Ontario, Canada")
    assert not g["pass"]
    assert g["score"] < 15


def test_geo_toronto_rejected():
    g = geo_score(hq="Toronto", city="Toronto", state="Ontario")
    assert g["reject"]
    assert not g["pass"]


def test_geo_serves_ottawa_not_enough():
    g = geo_score(extra_text="We serve Ottawa market from our Toronto office")
    # Mentions toronto without strong city → reject; if both, may soft-continue
    assert g["score"] < 15 or g["reject"]


def test_owner_founder_president_excellent():
    r = classify_owner_title("Founder & President")
    assert r["is_owner"] and r["ownership_confidence"] >= 0.9
    assert not r["reject"]


def test_owner_vp_sales_rejected():
    r = classify_owner_title("VP Sales")
    assert r["reject"]


def test_owner_regional_president_rejected():
    r = classify_owner_title("Regional President")
    assert r["reject"]


def test_ownership_pe_rejected():
    r = classify_ownership(text="Portfolio company of Apex Private Equity")
    assert r["reject"] and r["ownership_type"] == "private_equity"


def test_ownership_family_allowed_not_exit():
    r = classify_ownership(text="A family-owned manufacturer since 1987")
    assert not r["reject"]
    assert r["ownership_type"] == "family_owned"


def test_succession_family_alone_is_tier0():
    s = classify_succession_signals("Family-owned business in Ottawa")
    assert s["max_tier"] == 0
    assert s["recommendation"] == "context_only"
    assert s["succession_probability"] < 0.3


def test_succession_retirement_is_strong():
    s = classify_succession_signals(
        "Founder planning retirement; succession planning underway after 40 years"
    )
    assert s["max_tier"] >= 3
    assert s["succession_probability"] >= 0.7


def test_succession_counters_reduce():
    s = classify_succession_signals(
        "Leadership transition while expanding aggressively and opening new locations"
    )
    assert s["counter_signals"]
    assert s["succession_probability"] < 0.7


def test_intelligence_rejects_toronto():
    intel = build_company_intelligence({
        "company": "Big Corp",
        "contact_name": "Jane Doe",
        "contact_title": "Founder & President",
        "company_city": "Toronto",
        "employee_count": 80,
        "signal": "Family business considering succession planning",
    })
    q = qualify_keira_intelligence(intel)
    assert q["rejected"]
    assert not q["outreach_ok"]


def test_intelligence_good_ottawa_lead():
    intel = build_company_intelligence({
        "company": "ABC Manufacturing Inc.",
        "contact_name": "John Smith",
        "contact_title": "Founder & President",
        "company_city": "Ottawa",
        "employee_count": 85,
        "signal": (
            "Founded in 1987. John's daughter joined as VP Operations. "
            "Succession planning and next chapter language on website."
        ),
        "industry": "Industrial Manufacturing",
    })
    q = qualify_keira_intelligence(intel)
    assert not q["rejected"]
    assert q["score"] >= 50
    assert q.get("why_now")
    thesis = build_mna_thesis(intel, q)
    assert thesis["company"] == "ABC Manufacturing Inc."
    assert thesis["potential_owner"] == "John Smith"


def test_intelligence_family_only_not_outreach():
    intel = build_company_intelligence({
        "company": "Family Widgets",
        "contact_name": "Pat Lee",
        "contact_title": "Owner",
        "company_city": "Kingston",
        "employee_count": 40,
        "signal": "Family-owned since 1995",
    })
    q = qualify_keira_intelligence(intel)
    assert not q["rejected"]  # geo+owner ok
    assert not q["outreach_ok"]
    assert q["status"] == "research_required"


def test_pipeline_gates_without_seamless(tmp_db, monkeypatch):
    """Cheap gates run even when Seamless is unavailable."""
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
    gates = result["steps"]["gates"]
    assert gates["survivors"] >= 1
    assert gates["rejected"] >= 1
    assert "critic" in result["steps"]
