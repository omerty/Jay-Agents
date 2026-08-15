from src.agent import load_agent
from src.enrich import agent_size_score_boost, keira_size_score_boost, size_score_boost
from src.qualify import qualify_prospect


def test_keyword_qualify_strong_match():
    config = load_agent("woodway")
    q = qualify_prospect(
        config, "VP Data Governance at a large pharmaceutical company", use_llm=False
    )
    assert q["mode"] == "keywords"
    assert q["score"] >= 75
    assert q["tier"] == "hot"
    assert "pharma" in q["industries"]


def test_keyword_qualify_weak_match():
    config = load_agent("woodway")
    q = qualify_prospect(config, "Intern at a tiny bakery", use_llm=False)
    assert q["score"] < 50
    assert q["tier"] == "cold"


def test_keyword_qualify_sector_agnostic_keira():
    config = load_agent("keira")
    q = qualify_prospect(
        config, "Owner of an Ottawa manufacturer", use_llm=False, company="Ottawa Mfg Co"
    )
    assert q["mode"] == "keywords"
    assert q["score"] >= 50


def test_fonex_blocklist_penalizes_telco():
    config = load_agent("fonex")
    q = qualify_prospect(
        config,
        "VP Network Operations at Rogers Communications",
        use_llm=False,
        company="Rogers",
    )
    assert q["score"] <= 25
    assert any("blocklist" in r.lower() for r in q["reasons"])


def test_keira_blocklist_penalizes_broker():
    config = load_agent("keira")
    q = qualify_prospect(
        config,
        "Owner at Sunbelt Business Brokers",
        use_llm=False,
        company="Sunbelt",
    )
    assert q["score"] <= 25


def test_qualify_extra_appears_in_llm_prompt(monkeypatch):
    captured = {}

    def fake_chat_json(prompt, system=None, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = system
        return {
            "score": 80,
            "tier": "hot",
            "reasons": ["ok"],
            "talking_points": ["angle"],
            "recommendation": "Reach out",
        }

    monkeypatch.setattr("src.llm.chat_json", fake_chat_json)

    config = load_agent("woodway")
    config["prompts"]["qualify_extra"] = "ONLY score pharmaceutical companies above 90."
    qualify_prospect(config, "VP Privacy at Acme", use_llm=True, fallback=False)

    assert "ONLY score pharmaceutical companies above 90." in captured["system"]
    assert "SCORING NOTES" in captured["system"]
    assert "Woodway Assurance" in captured["system"]


def test_outreach_extra_appears_in_llm_prompt(monkeypatch):
    captured = {}

    def fake_chat(prompt, system=None, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = system
        return "Subject: Hi\n\nHi there,\n\nTest.\n\nBest,\nJay\nCo"

    monkeypatch.setattr("src.llm.chat", fake_chat)

    config = load_agent("fonex")
    config["prompts"]["outreach_extra"] = "Always mention DWDM upgrades in the opening sentence."
    from src.outreach import generate_outreach

    generate_outreach(
        config,
        "Director at Acme DC",
        {"score": 80, "tier": "hot", "talking_points": []},
        use_llm=True,
    )

    assert "Always mention DWDM upgrades" in captured["system"]
    assert "OUTREACH NOTES" in captured["system"]


def test_prompts_yaml_overrides_config(monkeypatch, tmp_path):
    import src.agent as agent_mod

    agents_dir = tmp_path / "agents" / "woodway"
    agents_dir.mkdir(parents=True)
    (agents_dir / "config.yaml").write_text(
        "name: woodway\ncompany: Woodway\nproduct: EviData\ntagline: Tag\n"
        "icp:\n  industries: [pharma]\n  titles: [VP Data Governance]\n  company_size: large\n"
        "prompts:\n  qualify_extra: From config file\n"
    )
    (agents_dir / "prompts.yaml").write_text("qualify_extra: From dashboard override\n")
    monkeypatch.setattr(agent_mod, "AGENTS_DIR", tmp_path / "agents")

    cfg = agent_mod.load_agent("woodway")
    assert cfg["prompts"]["qualify_extra"] == "From dashboard override"


def test_size_score_boost():
    assert size_score_boost(None) == 0
    assert size_score_boost(10000) == 15
    assert size_score_boost(1500) == 12
    assert size_score_boost(300) == 5
    assert size_score_boost(10) == -10
    assert size_score_boost(100) == 0


def test_woodway_size_score_boost_prefers_approachable_band():
    from src.enrich import woodway_size_score_boost

    assert woodway_size_score_boost(4000) == 15  # sweet spot
    assert woodway_size_score_boost(8000) == 15
    assert woodway_size_score_boost(1200) == 8  # above min, below prefer
    assert woodway_size_score_boost(12000) == 5  # above prefer — still OK
    assert woodway_size_score_boost(50000) == 5  # megabrand size — not blocked
    assert woodway_size_score_boost(200) == -10  # too small


def test_mega_brand_helpers():
    from src.enrich import is_mega_brand, mega_brand_demote_penalty

    cfg = {
        "mega_brand_prefer_peers": ["Bank of America", "Citi", "Eli Lilly"],
    }
    assert is_mega_brand("Bank of America", cfg) is True
    assert is_mega_brand("Eli Lilly and Company", cfg) is True
    # Must not match Citizens just because "Citi" is listed
    assert is_mega_brand("Citizens Financial Group", cfg) is False
    assert is_mega_brand("Regions Bank", cfg) is False
    # Demote is intentionally disabled — mix via discovery, don't score-kill
    assert mega_brand_demote_penalty("Bank of America", cfg) == 0


def test_keira_size_score_boost():
    assert keira_size_score_boost(200) == 15
    assert keira_size_score_boost(8000) == -15
    assert keira_size_score_boost(30) == 8


def test_agent_size_score_boost_routes_by_icp():
    from src.agent import load_agent

    woodway = load_agent("woodway")
    keira = load_agent("keira")
    # Woodway: approachable band boost; megas still get a small positive size nudge
    assert agent_size_score_boost(5000, woodway) == 15
    assert agent_size_score_boost(50000, woodway) == 5
    assert agent_size_score_boost(6000, keira) == -15


def test_woodway_does_not_hard_reject_megabrand_headcount():
    from src.icp_gates import max_employees, passes_icp_gates
    from src.agent import load_agent

    cfg = load_agent("woodway")
    assert max_employees(cfg) is None  # soft preference only
    ok_small, reason_small = passes_icp_gates(
        "Tiny Startup LLC", cfg, agent="woodway", employee_count=200, domain=None,
    )
    assert ok_small is False
    assert "below" in (reason_small or "")
    ok_mega, _reason_mega = passes_icp_gates(
        "Giant Bank Corp", cfg, agent="woodway", employee_count=80000, domain=None,
    )
    assert ok_mega is True  # megabrands allowed through the gate
