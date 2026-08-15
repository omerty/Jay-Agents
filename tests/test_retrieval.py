"""Tests for retrieval memory + query rotation."""

from src.db import active_domains, get_cursor, set_cursor
from src.discover import build_search_queries
from src.negative_list import is_excluded
from src.retrieval import build_cached_working_memory, build_context


def test_query_rotation_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "rot.db"))
    import importlib
    import src.db as db
    import src.discover as discover

    importlib.reload(db)
    importlib.reload(discover)
    db.init_db()

    config = {
        "name": "woodway",
        "icp": {"industries": ["pharma"], "geography": "global"},
        "discover": {
            "query_pool": [f"query-{i}" for i in range(20)],
            "search_signals": [],
        },
    }
    first = discover.build_search_queries(config, max_queries=8, agent="woodway")
    second = discover.build_search_queries(config, max_queries=8, agent="woodway")
    assert first == [f"query-{i}" for i in range(8)]
    assert second == [f"query-{i}" for i in range(8, 16)]
    assert get_cursor("woodway", "discover_query") == 16


def test_build_cached_memory_includes_exclusions():
    from src.agent import load_agent

    config = load_agent("woodway")
    block = build_cached_working_memory(config, task="qualify")
    assert "IDEAL CUSTOMER PROFILE" in block
    assert "LEARNED EXCLUSIONS" in block or "OneTrust" in block or "FEW-SHOT" in block
    assert "Pfizer" in block or "GOOD" in block


def test_build_context_per_company(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "ctx.db"))
    import importlib
    import src.db as db
    import src.retrieval as retrieval

    importlib.reload(db)
    importlib.reload(retrieval)
    db.init_db()
    db.upsert_lead({
        "company": "Pfizer",
        "contact_name": "Jane Doe",
        "contact_title": "CPO",
        "email": "jane.doe@pfizer.com",
        "company_domain": "pfizer.com",
        "status": "imported",
        "source": "test",
        "signal": "privacy hire",
    }, agent="woodway")
    ctx = retrieval.build_context("Pfizer", agent="woodway", domain="pfizer.com")
    assert "Jane Doe" in ctx
    assert "pfizer.com" in ctx.lower() or "Jane" in ctx


def test_known_funnel_for_discovery(tmp_db):
    from src.retrieval import known_funnel_for_discovery
    from src.db import upsert_lead

    upsert_lead({
        "company": "NHS England",
        "contact_name": "Jon Moore",
        "contact_title": "DPO",
        "email": "jon@england.nhs.uk",
        "status": "drafted",
        "source": "seamless",
    }, agent="woodway")
    block = known_funnel_for_discovery("woodway", hint_text="NHS England privacy officer hire")
    assert "FUNNEL MEMORY" in block
    assert "NHS England" in block
    assert "Jon Moore" in block
    # Unrelated catalog names must not bloat the prompt
    bloated = known_funnel_for_discovery("woodway", hint_text="unrelated widget search")
    assert "FUNNEL MEMORY" in bloated
    assert "NHS England" not in bloated


def test_active_domains_dedupe(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "dom.db"))
    import importlib
    import src.db as db

    importlib.reload(db)
    db.init_db()
    db.upsert_lead({
        "company": "Sanofi",
        "company_domain": "sanofi.com",
        "email": "a@sanofi.com",
        "status": "drafted",
        "source": "test",
    }, agent="woodway")
    domains = db.active_domains("woodway")
    assert "sanofi.com" in domains


def test_axtria_inc_still_blocked():
    assert is_excluded("Axtria, Inc.") == "blocklist"
