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
