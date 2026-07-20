import pytest

from src import contacts
from src.contacts import (
    ContactsError,
    _apollo_filters,
    _apollo_lead,
    resolve_contacts_provider,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("CONTACTS_PROVIDER", "APOLLO_API_KEY", "PDL_API_KEY", "HUNTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_provider_defaults_to_pdl():
    assert resolve_contacts_provider() == "pdl"


def test_provider_autodetects_apollo(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "ak_test")
    assert resolve_contacts_provider() == "apollo"


def test_provider_explicit_override(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "ak_test")
    monkeypatch.setenv("CONTACTS_PROVIDER", "pdl")
    assert resolve_contacts_provider() == "pdl"


def test_provider_unknown_raises(monkeypatch):
    monkeypatch.setenv("CONTACTS_PROVIDER", "zoominfo")
    with pytest.raises(ContactsError):
        resolve_contacts_provider()


def test_apollo_filters_fall_back_to_icp_titles():
    filters = _apollo_filters("woodway")
    assert filters["person_titles"]  # from apollo: block in config
    assert isinstance(filters["organization_num_employees_ranges"], list)


def test_apollo_lead_maps_fields():
    person = {
        "id": "abc123",
        "name": "Jane Doe",
        "first_name": "Jane",
        "last_name": "Doe",
        "title": "Chief Privacy Officer",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "organization": {
            "name": "Acme Pharma",
            "primary_domain": "www.acmepharma.com",
            "estimated_num_employees": 4200,
            "industry": "pharmaceuticals",
        },
    }
    lead = _apollo_lead(person)
    assert lead["company"] == "Acme Pharma"
    assert lead["contact_name"] == "Jane Doe"
    assert lead["contact_title"] == "Chief Privacy Officer"
    assert lead["email"] is None
    assert lead["employee_count"] == 4200
    assert lead["source"] == "apollo"
    assert lead["_domain"] == "acmepharma.com"
    assert lead["_apollo_id"] == "abc123"


def test_apollo_lead_requires_company():
    assert _apollo_lead({"name": "Jane", "organization": {}}) is None


def test_apollo_search_and_import(tmp_db, monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "ak_test")

    people = [
        {
            "id": f"p{i}",
            "name": f"Person {i}",
            "first_name": "Person",
            "last_name": str(i),
            "title": "VP Data Governance",
            "organization": {"name": f"Company {i}", "estimated_num_employees": 2000},
        }
        for i in range(3)
    ]

    class FakeApollo:
        def __init__(self, *a, **kw):
            pass

        def search_people(self, *, page=1, per_page=25, **filters):
            return {
                "people": people if page == 1 else [],
                "pagination": {"total_entries": 3, "total_pages": 1},
            }

    import src.apollo_api

    monkeypatch.setattr(src.apollo_api, "ApolloClient", FakeApollo)

    result = contacts.search_and_import_contacts("woodway", limit=10)
    assert result["provider"] == "apollo"
    assert result["imported"] == 3
    assert result["credits_used"] == 0

    rows = tmp_db.get_leads(agent="woodway")
    assert len(rows) == 3
    assert all(r["status"] == "imported" for r in rows)

    # re-running skips duplicates
    result2 = contacts.search_and_import_contacts("woodway", limit=10)
    assert result2["imported"] == 0
    assert result2["skipped"] == 3


def test_contacts_status_shape(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "ak_test")
    s = contacts.contacts_status()
    assert s == {"provider": "apollo", "configured": True, "email_finder": False}
