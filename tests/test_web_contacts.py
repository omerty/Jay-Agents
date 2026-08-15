"""Tests for web contact discovery."""

from unittest.mock import patch


def test_web_contacts_extract_and_import(tmp_db, monkeypatch):
    monkeypatch.setenv("WEB_CONTACT_DELAY", "0")
    monkeypatch.setenv("WEB_CONTACT_QUERIES", "1")

    hits = [{
        "query": "test",
        "title": "Jane Doe - Chief Privacy Officer - Acme Pharma | LinkedIn",
        "url": "https://www.linkedin.com/in/janedoe",
        "snippet": "Jane Doe is Chief Privacy Officer at Acme Pharma",
    }]

    def fake_search(query, max_results=5):
        return hits

    def fake_chat_json(prompt, system=None, **kwargs):
        return {
            "contacts": [{
                "contact_name": "Jane Doe",
                "contact_title": "Chief Privacy Officer",
                "linkedin_url": "https://linkedin.com/in/janedoe",
                "signal": "CPO at target company",
            }]
        }

    with patch("src.web_contacts.search_web", fake_search):
        with patch("src.web_contacts.chat_json", fake_chat_json):
            from src.web_contacts import search_and_import_web_contacts

            result = search_and_import_web_contacts(
                "woodway",
                ["Acme Pharma"],
                limit=5,
                skip_existing=False,
            )

    assert result["provider"] == "web_research"
    assert result["imported"] == 1
    assert result["with_linkedin"] == 1

    with tmp_db.get_connection() as conn:
        row = conn.execute(
            "SELECT contact_name, linkedin_url, source FROM leads WHERE company = ?",
            ("Acme Pharma",),
        ).fetchone()
    assert row["contact_name"] == "Jane Doe"
    assert "linkedin.com" in row["linkedin_url"]
    assert row["source"] == "web_research"
