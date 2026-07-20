from src.agent import load_agent
from src.outreach import _clean_email, _extract_hints, generate_outreach


def test_extract_hints_from_contact():
    name, company = _extract_hints(
        "irrelevant", {"contact_name": "Jane Doe", "company": "Acme"}
    )
    assert name == "Jane"
    assert company == "Acme"


def test_extract_hints_from_prospect_string():
    name, company = _extract_hints("Sarah Chen, VP Data Governance at BigPharma Co")
    assert name == "Sarah"
    assert company == "BigPharma Co"


def test_extract_hints_company_only():
    name, company = _extract_hints("Acme Widgets")
    assert name == "there"
    assert company == "Acme Widgets"


def test_mock_outreach_uses_template():
    config = load_agent("woodway")
    out = generate_outreach(
        config,
        "Jane Doe, CPO at Acme",
        {"score": 80, "tier": "hot", "talking_points": []},
        use_llm=False,
        contact={"contact_name": "Jane Doe", "company": "Acme", "email": "j@acme.com"},
    )
    assert out["mode"] == "mock"
    assert "Jane" in out["body"]
    assert out["to_email"] == "j@acme.com"
    assert out["subject"]


def test_clean_email_strips_preamble():
    raw = "Sure! Here's your email:\nSubject: Hello\n\nHi Jane,\nBody"
    cleaned = _clean_email(raw)
    assert cleaned.startswith("Subject: Hello")
