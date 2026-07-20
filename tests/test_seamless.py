"""Tests for Seamless API integration."""

import json

import pytest

from src.seamless import (
    _in_target_geography,
    _lead_from_research,
    _lead_from_search,
    _preview_score,
    seamless_filters,
    search_and_import_seamless,
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
    monkeypatch.setenv("SEAMLESS_MONTHLY_BUDGET", "10000")
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")


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
    assert "10+ Years" in filters["companyFoundedOn"]
    assert "$5M - $20M" in filters["companyRevenue"]


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
    ok, _ = can_research(3)
    assert ok
    ok, msg = can_research(10)
    assert not ok
    assert "Run limit" in msg


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
    monkeypatch.setenv("SEAMLESS_KEIRA_RESEARCH_LIMIT", "8")
    assert max_research_per_run("keira") == 8
