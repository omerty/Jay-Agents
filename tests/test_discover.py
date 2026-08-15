from src.agent import load_agent
from src.discover import _filter_leads, _should_skip, build_search_queries


def test_build_queries_respects_explicit_list():
    config = load_agent("keira")
    queries = build_search_queries(config, max_queries=3)
    assert len(queries) == 3
    assert all(isinstance(q, str) for q in queries)


def test_build_queries_from_signals():
    config = load_agent("woodway")
    queries = build_search_queries(config, max_queries=5)
    assert len(queries) == 5


def test_should_skip_blocklist_and_length():
    assert _should_skip("LinkedIn")
    assert _should_skip("ab")
    assert _should_skip("x" * 60)
    assert not _should_skip("Acme Widgets")


def test_should_skip_agent_blocklist():
    config = load_agent("fonex")
    assert _should_skip("Rogers Communications", config)
    assert not _should_skip("Acme Data Centres", config)


def test_filter_leads_dedupes_and_fills_contact():
    from src.discover import looks_like_company_name

    assert looks_like_company_name("Pfizer")
    assert not looks_like_company_name("What")
    assert not looks_like_company_name("Mercy Health Joe Nguyen")

    config = load_agent("woodway")
    leads = _filter_leads(
        [
            {"company": "Acme"},
            {"company": "acme"},
            {"company": "LinkedIn"},
            {"company": ""},
        ],
        config,
    )
    assert len(leads) == 1
    assert "at Acme" in leads[0]["suggested_contact"]


def test_extract_pipeline_batches_hits_and_skips_qualify_extra(monkeypatch):
    """All hit batches are sent to the LLM; qualify_extra scoring notes are not injected."""
    from src.discover import _extract_companies_for_pipeline

    config = load_agent("woodway")
    assert config["prompts"].get("qualify_extra")  # present in config, must not leak into extract

    prompts_seen: list[str] = []
    call_n = {"n": 0}

    def fake_chat_json(prompt, system=None, **kwargs):
        prompts_seen.append(prompt)
        call_n["n"] += 1
        # First batch → Co1; second → Co2
        if call_n["n"] == 1:
            return {"leads": [{"company": "BatchOne Bank", "industry": "banking", "signal": "hit"}]}
        return {"leads": [{"company": "BatchTwo Health", "industry": "healthcare", "signal": "hit"}]}

    monkeypatch.setattr("src.llm.chat_json", fake_chat_json)

    hits = []
    for i in range(45):
        hits.append({
            "query": f"q{i}",
            "title": f"Title {i}",
            "snippet": f"Company mention {i}",
            "url": f"https://example.com/{i}",
        })

    rows = _extract_companies_for_pipeline(hits, config, limit=50)
    assert call_n["n"] >= 2  # batched beyond 40
    assert all("SCORING NOTES" not in p for p in prompts_seen)
    assert all("score ≤25" not in p and "0–100" not in p for p in prompts_seen)
    names = {r["company"] for r in rows}
    assert "BatchOne Bank" in names
    assert "BatchTwo Health" in names


def test_woodway_discover_config_targets_50():
    config = load_agent("woodway")
    disc = config.get("discover") or {}
    assert disc.get("default_limit") == 50
    assert disc.get("max_queries") == 20
