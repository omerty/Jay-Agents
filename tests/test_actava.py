"""Tests for Actava integration."""

import pytest

from src.actava import (
    _keira_prospect_prompt,
    _lead_record,
    _parse_leads_json,
    search_and_import_actava,
)
from src.actava_api import actava_available, actava_agent_configured, extract_response_text


@pytest.fixture(autouse=True)
def clean_actava_env(monkeypatch):
    monkeypatch.delenv("ACTAVA_API_KEY", raising=False)
    monkeypatch.delenv("ACTAVA_AGENT_ID", raising=False)


def test_actava_not_available_without_key():
    assert actava_available() is False


def test_actava_rejects_placeholder_key(monkeypatch):
    monkeypatch.setenv("ACTAVA_API_KEY", "your_key_here")
    assert actava_available() is False


def test_actava_available_with_key(monkeypatch):
    monkeypatch.setenv("ACTAVA_API_KEY", "ak_live_test")
    assert actava_available() is True


def test_agent_mode_requires_agent_id(monkeypatch):
    monkeypatch.setenv("ACTAVA_API_KEY", "ak_live_test")
    assert actava_agent_configured() is False
    monkeypatch.setenv("ACTAVA_AGENT_ID", "agent-123")
    assert actava_agent_configured() is True


def test_extract_response_text_variants():
    assert extract_response_text({"output_text": "from session"}) == "from session"
    assert extract_response_text({"output": "hello"}) == "hello"
    assert extract_response_text({"message": {"content": "nested"}}) == "nested"
    assert extract_response_text({"choices": [{"message": {"content": "x"}}]}) == "x"


def test_parse_leads_json():
    leads = _parse_leads_json('{"leads": [{"company": "Acme Co", "signal": "owner retiring"}]}')
    assert len(leads) == 1
    assert leads[0]["company"] == "Acme Co"


def test_parse_leads_json_after_narrative():
    text = (
        "Research notes about Ottawa manufacturers...\n\n"
        '{"leads": [{"company": "Metal Works", "contact_name": "Jane", "signal": "succession"}]}'
    )
    leads = _parse_leads_json(text)
    assert len(leads) == 1
    assert leads[0]["company"] == "Metal Works"


def test_lead_record_maps_fields():
    cfg = {"blocklist": [], "icp": {}}
    lead = _lead_record(
        {
            "company": "Ottawa Metal Works",
            "contact_name": "Jane Owner",
            "contact_title": "President",
            "signal": "family business succession",
        },
        cfg,
    )
    assert lead["company"] == "Ottawa Metal Works"
    assert lead["source"] == "actava"
    assert "succession" in lead["signal"]


def test_keira_prospect_prompt_mentions_ottawa():
    from src.agent import load_agent

    prompt = _keira_prospect_prompt(load_agent("keira"), limit=5)
    assert "Ottawa" in prompt or "Eastern Ontario" in prompt
    assert "leads" in prompt


def test_search_and_import_discover_cura_mock(tmp_db, monkeypatch):
    monkeypatch.setenv("ACTAVA_API_KEY", "ak_live_test")

    hits = [
        {
            "query": "Ottawa manufacturer owner",
            "title": "Smith Manufacturing — family owned since 1985",
            "snippet": "Ottawa-based Smith Manufacturing, founded by John Smith, serves Eastern Ontario.",
        }
    ]

    class FakeCura:
        @staticmethod
        def cura_chat_json(prompt, *, system=None):
            return {
                "leads": [
                    {
                        "company": "Smith Manufacturing",
                        "contact_name": "John Smith",
                        "contact_title": "Owner",
                        "signal": "Family-owned manufacturer in Ottawa since 1985",
                    }
                ]
            }

    monkeypatch.setattr("src.actava.discover_raw_hits", lambda *a, **k: hits)
    monkeypatch.setattr("src.actava.cura_chat_json", FakeCura.cura_chat_json)

    result = search_and_import_actava("keira", limit=5)
    assert result["mode"] == "discover+cura"
    assert result["imported"] == 1


def test_search_and_import_agent_mock(tmp_db, monkeypatch):
    monkeypatch.setenv("ACTAVA_API_KEY", "ak_live_test")
    monkeypatch.setenv("ACTAVA_AGENT_ID", "agent-abc")

    def fake_run(agent_id, message, *, extra=None, on_progress=None):
        assert agent_id == "agent-abc"
        if on_progress:
            on_progress("Actava run started")
        return {
            "output_text": '{"leads": [{"company": "Leblanc HVAC", "contact_name": "Robert Leblanc", "contact_title": "Owner", "signal": "long-tenured owner"}]}'
        }

    monkeypatch.setattr("src.actava.run_agent_and_wait", fake_run)

    result = search_and_import_actava("keira", limit=3)
    assert result["mode"] == "agent"
    assert result["imported"] == 1
