from src.contact_channel import contact_channel_info, enrich_lead


def test_email_channel_when_address_present():
    lead = {"email": "glenn@example.com", "linkedin_url": "https://linkedin.com/in/glenn"}
    info = contact_channel_info(lead)
    assert info["channel"] == "email"
    assert info["can_gmail"] is True
    assert info["message"] is None


def test_linkedin_channel_when_named_contact_has_no_email():
    lead = {
        "company": "intact public entities",
        "contact_name": "glenn minnis",
        "email": None,
        "linkedin_url": "linkedin.com/in/glennminnis",
    }
    info = contact_channel_info(lead)
    assert info["channel"] == "linkedin"
    assert info["label"] == "Email not found"
    assert "LinkedIn" in info["message"]
    assert info["can_gmail"] is False


def test_incomplete_when_name_without_email_or_linkedin():
    lead = {"contact_name": "glenn minnis", "email": "", "linkedin_url": ""}
    info = contact_channel_info(lead)
    assert info["channel"] == "incomplete"
    assert info["can_gmail"] is False


def test_company_only_when_no_named_contact():
    lead = {"company": "intact public entities", "email": None, "linkedin_url": None}
    info = contact_channel_info(lead)
    assert info["channel"] == "company_only"


def test_enrich_lead_attaches_api_fields():
    lead = {"id": 1, "contact_name": "glenn minnis", "linkedin_url": "https://li/in/g"}
    enriched = enrich_lead(lead)
    assert enriched["contact_channel"] == "linkedin"
    assert enriched["contact_label"]
    assert enriched["contact_message"]
    assert enriched["can_gmail"] is False
    assert enriched["id"] == 1


def test_can_hunter_research_when_configured(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    lead = {"contact_name": "glenn minnis", "email": None, "linkedin_url": "https://li/in/g"}
    enriched = enrich_lead(lead)
    assert enriched["can_hunter_research"] is True


def test_can_hunter_research_false_when_email_present(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    lead = {"contact_name": "glenn minnis", "email": "g@example.com"}
    enriched = enrich_lead(lead)
    assert enriched["can_hunter_research"] is False
