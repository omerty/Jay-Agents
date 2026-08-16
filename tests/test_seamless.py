"""Tests for Seamless API integration."""

import json

import pytest

from src.seamless import (
    SEAMLESS_MAX_COMPANIES_PER_SEARCH,
    _in_target_geography,
    _lead_from_research,
    _lead_from_search,
    _preview_score,
    seamless_filters,
    search_and_import_seamless,
    search_and_import_seamless_for_companies,
)
from src.seamless_api import SeamlessClient, get_seamless_client, seamless_available, seamless_transport
from src.seamless_mcp import SeamlessMcpClient
from src.seamless_budget import (
    budget_status,
    can_research,
    credits_used_this_month,
    max_research_per_run,
    record_credits_used,
)


@pytest.fixture(autouse=True)
def clean_seamless_env(monkeypatch):
    monkeypatch.delenv("SEAMLESS_API_KEY", raising=False)
    monkeypatch.setenv("SEAMLESS_TRANSPORT", "rest")
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "1000")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")


def test_seamless_not_available_without_key():
    assert seamless_available() is False


def test_seamless_available_with_key(monkeypatch):
    monkeypatch.setenv("SEAMLESS_API_KEY", "sk_live_real_key")
    assert seamless_available() is True


def test_keira_seamless_filters():
    filters = seamless_filters("keira")
    assert "Canada" in filters["contactCountry"]
    assert "Ontario" in filters["contactState"]
    assert "Owner" in filters["jobTitle"]
    assert filters.get("seniority") == ["C-Level"]
    # Widened discovery: no succession contactKeyword / revenue mega-filters on base
    assert "contactKeyword" not in filters
    assert "companyRevenue" not in filters
    assert "10+ Years" in filters["companyFoundedOn"]


def test_keira_discovery_query_city_overlay():
    ottawa = seamless_filters(
        "keira",
        query={
            "name": "ottawa_corridor",
            "company_city": ["Ottawa", "Kanata"],
            "omit": ["industry", "company_revenue", "company_founded_on", "contact_keyword"],
        },
    )
    assert ottawa["companyCity"] == ["Ottawa", "Kanata"]
    assert "companyFoundedOn" not in ottawa
    assert "companyRevenue" not in ottawa
    assert "contactKeyword" not in ottawa
    assert "Owner" in ottawa["jobTitle"]


def test_keira_discovery_queries_configured():
    from src.seamless import _keira_discovery_queries

    qs = _keira_discovery_queries("keira")
    names = [q["name"] for q in qs]
    assert "ontario_owners" in names
    assert "ottawa_corridor" in names
    assert "corridor_towns" in names
    assert "kingston_owners" in names
    assert len(qs) >= 5


def test_preview_score_prioritizes_owners():
    owner = {"title": "Owner & Founder", "employeeSizeRange": "51 - 200", "companyFoundedOn": "10+ Years",
             "companyCity": "Ottawa", "companyState": "Ontario", "companyCountry": "Canada"}
    manager = {"title": "Operations Manager", "employeeSizeRange": "51 - 200", "companyCity": "Ottawa",
               "companyState": "Ontario", "companyCountry": "Canada"}
    assert _preview_score(owner) > _preview_score(manager)


def test_geography_filter():
    assert _in_target_geography({"companyCity": "Ottawa", "companyCountry": "Canada"})
    assert not _in_target_geography({"companyCity": "Vancouver", "companyState": "BC", "companyCountry": "Canada"})


def test_lead_from_search_maps_fields():
    lead = _lead_from_search({
        "searchResultId": "abc",
        "name": "Jane Owner",
        "company": "ABC Manufacturing",
        "title": "President",
        "liUrl": "https://linkedin.com/in/jane",
        "employeeSizeRange": "51 - 200",
        "industries": ["Manufacturing"],
    })
    assert lead["company"] == "ABC Manufacturing"
    assert lead["contact_name"] == "Jane Owner"
    assert lead["employee_count"] == 125
    assert lead["source"] == "seamless"


def test_lead_from_research_maps_email():
    lead = _lead_from_research(
        {"fullName": "Bob Smith", "company": "Smith HVAC", "title": "Owner", "email": "bob@smithhvac.ca"},
        preview={"companyFoundedOn": "1989-01-01", "companyRevenue": "$5M - $20M"},
    )
    assert lead["email"] == "bob@smithhvac.ca"
    assert "Founded" in lead["signal"]


def test_budget_tracking(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "5")
    assert credits_used_this_month() == 0
    record_credits_used(3, remaining=9997)
    assert credits_used_this_month() == 3
    status = budget_status(9997)
    assert status["credits_remaining_budget"] == 9997
    assert status["api_credits_remaining"] == 9997


def test_can_research_respects_limits(monkeypatch):
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "5")
    monkeypatch.setenv("SEAMLESS_DAILY_HARD_CAP", "250")
    ok, _ = can_research(3)
    assert ok
    ok, _ = can_research(10)  # within per-run 5 + overage 20
    assert ok
    ok, msg = can_research(40)
    assert not ok
    assert "capped" in msg.lower() or "budget" in msg.lower() or "hard" in msg.lower()


def test_kill_switch_blocks_research(monkeypatch):
    from src.seamless_budget import allocate_research_slots

    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "false")
    ok, msg = can_research(1)
    assert not ok
    assert "disabled" in msg.lower()
    n, reason = allocate_research_slots(5, agent="keira")
    assert n == 0
    assert "disabled" in reason.lower()


def test_daily_budget_blocks(tmp_db, monkeypatch):
    from src.seamless_budget import allocate_research_slots, credits_used_today

    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "5")
    monkeypatch.setenv("SEAMLESS_DAILY_HARD_CAP", "5")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")
    record_credits_used(5)
    assert credits_used_today() == 5
    ok, msg = can_research(1, agent="keira")
    assert not ok
    assert "daily" in msg.lower()
    n, _ = allocate_research_slots(3, agent="keira")
    assert n == 0


def test_notify_seamless_budget_exhausted_creates_notification(tmp_db, monkeypatch):
    from src.seamless_budget import (
        BUDGET_ALERT_MARKER,
        _BUDGET_NOTIFY_KEYS,
        notify_seamless_budget_exhausted,
    )
    from src.db import get_notifications

    _BUDGET_NOTIFY_KEYS.clear()
    alert = notify_seamless_budget_exhausted(
        "keira daily Seamless budget exhausted (44/44 used today)",
        agent="keira",
        context="pipeline start",
    )
    assert alert["type"] == "seamless_budget_exhausted"
    assert alert["notified"] is True
    assert BUDGET_ALERT_MARKER in alert["message"]

    notifs = get_notifications(unread_only=True, limit=10)
    assert any(BUDGET_ALERT_MARKER in (n.get("message") or "") for n in notifs)

    # Dedupe — second call should not notify again
    alert2 = notify_seamless_budget_exhausted(
        "keira daily Seamless budget exhausted (44/44 used today)",
        agent="keira",
        context="pipeline start",
    )
    assert alert2["notified"] is False


def test_allocate_caps_to_remaining(tmp_db, monkeypatch):
    from src.seamless_budget import allocate_research_slots

    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "10")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")
    record_credits_used(7)
    n, note = allocate_research_slots(5, agent="keira")
    assert n == 3
    assert "capped" in note


def test_api_reserve_floor(monkeypatch):
    from src.seamless_budget import allocate_research_slots

    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "50")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")
    n, reason = allocate_research_slots(5, agent="keira", api_remaining=52)
    assert n == 2  # 52 - 50 reserve
    n2, _ = allocate_research_slots(5, agent="keira", api_remaining=40)
    assert n2 == 0


def test_search_and_import_seamless_mock(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_API_KEY", "sk_live_real_key")

    search_response = {
        "data": [
            {
                "searchResultId": "sr1",
                "name": "Alice Founder",
                "company": "Ottawa Metal Works",
                "title": "Owner",
                "companyCity": "Ottawa",
                "companyState": "Ontario",
                "companyCountry": "Canada",
                "employeeSizeRange": "51 - 200",
                "companyFoundedOn": "10+ Years",
            },
            {
                "searchResultId": "sr2",
                "name": "Broker Guy",
                "company": "Sunbelt Business Brokers",
                "title": "Owner",
                "companyCity": "Ottawa",
                "companyState": "Ontario",
                "companyCountry": "Canada",
            },
        ],
    }
    poll_response = [{
        "requestId": "req1",
        "searchResultId": "sr1",
        "status": "done",
        "contact": {
            "fullName": "Alice Founder",
            "company": "Ottawa Metal Works",
            "title": "Owner",
            "email": "alice@ottawametal.ca",
            "liUrl": "https://linkedin.com/in/alice",
        },
    }]

    class FakeClient:
        last_credits = 9999

        def search_contacts(self, filters, *, limit=50, next_token=None):
            return search_response

        def research_contacts(self, ids):
            assert ids == ["sr1"]
            return ["req1"]

        def wait_for_contact_research(self, request_ids):
            return poll_response

    monkeypatch.setattr("src.seamless.get_seamless_client", lambda: FakeClient())

    result = search_and_import_seamless("keira", limit=5)
    assert result["imported"] == 1
    assert result["with_email"] == 1
    assert result["credits_used"] == 1
    assert result["skipped"] >= 1  # broker blocklisted


def test_seamless_transport_default_rest():
    assert seamless_transport() == "rest"


def test_get_seamless_client_mcp(monkeypatch):
    monkeypatch.setenv("SEAMLESS_API_KEY", "mcp_key")
    monkeypatch.setenv("SEAMLESS_TRANSPORT", "mcp")
    client = get_seamless_client()
    assert isinstance(client, SeamlessMcpClient)


def test_mcp_parse_tool_json_payload():
    client = SeamlessMcpClient(api_key="test")
    payload = client._parse_tool_payload(
        {
            "content": [
                {"type": "text", "text": '{"data": [{"searchResultId": "x", "company": "Acme"}]}'}
            ]
        }
    )
    assert payload["data"][0]["company"] == "Acme"


def test_search_and_import_seamless_mcp_mock(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_API_KEY", "mcp_key")
    monkeypatch.setenv("SEAMLESS_TRANSPORT", "mcp")

    class FakeMcpClient:
        last_credits = 5000

        def search_contacts(self, filters, *, limit=50, next_token=None):
            return {
                "data": [{
                    "searchResultId": "sr1",
                    "name": "Bob Owner",
                    "company": "Kingston Logistics",
                    "title": "President",
                    "companyCity": "Kingston",
                    "companyState": "Ontario",
                    "companyCountry": "Canada",
                    "employeeSizeRange": "51 - 200",
                }]
            }

        def research_and_wait_contacts(self, ids):
            return [{
                "requestId": "req1",
                "searchResultId": "sr1",
                "status": "done",
                "contact": {
                    "fullName": "Bob Owner",
                    "company": "Kingston Logistics",
                    "title": "President",
                    "email": "bob@kingstonlogistics.ca",
                },
            }]

    monkeypatch.setattr("src.seamless.get_seamless_client", lambda: FakeMcpClient())
    result = search_and_import_seamless("keira", limit=3)
    assert result["imported"] == 1
    assert result["with_email"] == 1


def test_seamless_client_raises_without_key():
    with pytest.raises(Exception):
        SeamlessClient(api_key="")
    with pytest.raises(Exception):
        SeamlessMcpClient(api_key="")


def test_max_research_per_run_keira(monkeypatch):
    monkeypatch.setenv("SEAMLESS_CAPS_DISABLED", "false")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")
    assert max_research_per_run("keira") == 8


def test_caps_disabled_skips_budget_clamps(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_CAPS_DISABLED", "true")
    monkeypatch.setenv("SEAMLESS_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "1")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "1")
    from src.seamless_budget import allocate_research_slots, caps_disabled, max_research_per_run

    assert caps_disabled()
    assert max_research_per_run("keira") >= 1000
    n, note = allocate_research_slots(80, agent="keira")
    assert n == 80
    assert "caps disabled" in note.lower()


def test_max_research_per_run_woodway_default_50(monkeypatch):
    monkeypatch.setenv("SEAMLESS_CAPS_DISABLED", "false")
    monkeypatch.delenv("SEAMLESS_WOODWAY_RESEARCH_LIMIT", raising=False)
    assert max_research_per_run("woodway") == 50


def test_pick_coverage_first_one_per_company_before_seconds():
    from src.seamless import pick_coverage_first

    # High-scoring CoA contacts first — without coverage-first we'd pick 2×CoA before CoB
    ranked = [
        (100, {"company": "CoA", "searchResultId": "a1", "title": "VP"}),
        (99, {"company": "CoA", "searchResultId": "a2", "title": "Director"}),
        (50, {"company": "CoB", "searchResultId": "b1", "title": "CPO"}),
        (40, {"company": "CoC", "searchResultId": "c1", "title": "VP"}),
    ]
    picked, per = pick_coverage_first(ranked, max_per_company=2, research_cap=3)
    ids = [item["searchResultId"] for _, item in picked]
    assert ids == ["a1", "b1", "c1"]
    assert per == {"coa": 1, "cob": 1, "coc": 1}

    picked2, per2 = pick_coverage_first(ranked, max_per_company=2, research_cap=4)
    ids2 = [item["searchResultId"] for _, item in picked2]
    assert ids2 == ["a1", "b1", "c1", "a2"]
    assert per2["coa"] == 2


def test_company_search_batches_at_15(tmp_db, monkeypatch):
    """Seamless API rejects companyName lists longer than 15."""
    monkeypatch.setenv("SEAMLESS_API_KEY", "sk_live_real_key")
    monkeypatch.setenv("SEAMLESS_WOODWAY_RESEARCH_LIMIT", "3")
    assert SEAMLESS_MAX_COMPANIES_PER_SEARCH == 15

    company_names = [f"Co{i}" for i in range(24)]
    batch_sizes: list[int] = []

    class FakeClient:
        last_credits = 9999

        def search_contacts(self, filters, *, limit=50, next_token=None):
            names = filters.get("companyName") or []
            assert len(names) <= SEAMLESS_MAX_COMPANIES_PER_SEARCH
            batch_sizes.append(len(names))
            # One hit per company in the batch
            return {
                "data": [
                    {
                        "searchResultId": f"sr-{n}",
                        "name": f"Person {n}",
                        "company": n,
                        "title": "VP Data Governance",
                        "employeeSizeRange": "10001+",
                    }
                    for n in names
                ]
            }

        def research_contacts(self, ids):
            return [f"req-{i}" for i in ids]

        def wait_for_contact_research(self, request_ids):
            return [
                {
                    "requestId": rid,
                    "searchResultId": rid.replace("req-", "sr-"),
                    "status": "done",
                    "contact": {
                        "fullName": "Pat Contact",
                        "company": "Acme",
                        "title": "VP Data Governance",
                        "email": "pat@example.com",
                    },
                }
                for rid in request_ids
            ]

    monkeypatch.setattr("src.seamless.get_seamless_client", lambda: FakeClient())
    result = search_and_import_seamless_for_companies(
        "woodway", company_names, limit=3,
    )
    assert batch_sizes == [15, 9]
    assert result["searched"] == 24
    assert result["imported"] >= 1
    assert result["provider"] == "seamless"


def test_company_search_caps_people_per_company(tmp_db, monkeypatch):
    monkeypatch.setenv("SEAMLESS_API_KEY", "sk_live_real_key")
    monkeypatch.setenv("SEAMLESS_WOODWAY_RESEARCH_LIMIT", "20")
    monkeypatch.setenv("SEAMLESS_WOODWAY_CONTACTS_PER_COMPANY", "2")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET", "100")
    monkeypatch.setenv("SEAMLESS_DAILY_BUDGET_WOODWAY", "100")
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_MIN_RESERVE", "0")

    researched: list[str] = []

    class FakeClient:
        last_credits = 9999

        def search_contacts(self, filters, *, limit=50, next_token=None):
            return {
                "data": [
                    {
                        "searchResultId": f"sr-{i}",
                        "name": f"Person {i}",
                        "company": "NHS England",
                        "title": "VP Data Governance",
                        "employeeSizeRange": "10001+",
                    }
                    for i in range(10)
                ]
            }

        def research_contacts(self, ids):
            researched.extend(ids)
            return [f"req-{i}" for i in ids]

        def wait_for_contact_research(self, request_ids):
            return [
                {
                    "requestId": rid,
                    "searchResultId": rid.replace("req-", "sr-"),
                    "status": "done",
                    "contact": {
                        "fullName": "Pat Contact",
                        "company": "NHS England",
                        "title": "VP Data Governance",
                        "email": f"{rid}@england.nhs.uk",
                    },
                }
                for rid in request_ids
            ]

    monkeypatch.setattr("src.seamless.get_seamless_client", lambda: FakeClient())
    from src.seamless import search_and_import_seamless_for_companies

    result = search_and_import_seamless_for_companies(
        "woodway", ["NHS England"], limit=20,
    )
    assert len(researched) == 2
    assert result["researched"] == 2
