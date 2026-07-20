from src.pdl import (
    _lead_from_pdl_person,
    _normalize_row,
    _parse_int,
    build_search_query,
)


def test_parse_int_variants():
    assert _parse_int("1,234") == 1234
    assert _parse_int("1001-5000") == 3000  # size-range midpoint
    assert _parse_int("10001+") == 15000
    assert _parse_int("") is None
    assert _parse_int(None) is None


def test_normalize_row_maps_columns():
    row = {
        "First Name": "Jane",
        "Last Name": "Doe",
        "Job Title": "CPO",
        "Company Name": "Acme",
        "Work Email": "j@acme.com",
        "Job Company Size": "1001-5000",
    }
    out = _normalize_row(row)
    assert out["contact_name"] == "Jane Doe"
    assert out["contact_title"] == "CPO"
    assert out["company"] == "Acme"
    assert out["email"] == "j@acme.com"
    assert out["employee_count"] == 3000


def test_lead_from_pdl_person_skips_obscured_fields():
    person = {
        "job_company_name": "Acme",
        "full_name": "Jane Doe",
        "job_title": "Chief Privacy Officer",
        "work_email": True,  # PDL free plans return booleans for hidden fields
        "linkedin_url": "linkedin.com/in/janedoe",
        "job_company_size": "1001-5000",
        "industry": "pharma",
    }
    lead = _lead_from_pdl_person(person)
    assert lead["email"] is None
    assert lead["company"] == "Acme"
    assert lead["employee_count"] == 3000


def test_lead_from_pdl_person_requires_company():
    assert _lead_from_pdl_person({"full_name": "Jane"}) is None


def test_build_search_query_shape():
    q = build_search_query({
        "job_title_keywords": ["privacy"],
        "job_company_sizes": ["10001+"],
        "job_title_levels": ["vp"],
        "require_work_email": True,
        "location_country": "Canada",
        "location_region": None,
        "industries": [],
        "filter_industries": False,
    })
    must = q["bool"]["must"]
    assert {"terms": {"job_company_size": ["10001+"]}} in must
    assert {"exists": {"field": "work_email"}} in must
    assert {"term": {"location_country": "canada"}} in must
