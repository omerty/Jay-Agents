"""Woodway domain resolution regression — avoid wrong recipients / wasted credits."""

from __future__ import annotations

import pytest

from src.email_patterns import resolve_company_domain, resolve_company_domain_with_confidence


@pytest.mark.parametrize(
    "company,expected",
    [
        ("Roche", "roche.com"),
        ("F. Hoffmann-La Roche", "roche.com"),
        ("Hoffmann-La Roche", "roche.com"),
        ("Roche Diagnostics", "roche.com"),
        ("Pfizer", "pfizer.com"),
        ("Novartis", "novartis.com"),
        ("AstraZeneca", "astrazeneca.com"),
        ("Johnson & Johnson", "jnj.com"),
        ("J&J", "jnj.com"),
        ("Merck", "merck.com"),
        ("Sanofi", "sanofi.com"),
        ("GSK", "gsk.com"),
        ("GlaxoSmithKline", "gsk.com"),
        ("Bank of America", "bankofamerica.com"),
        ("JPMorgan Chase", "jpmorganchase.com"),
        ("UnitedHealth Group", "unitedhealthgroup.com"),
    ],
)
def test_known_enterprise_domains(company, expected):
    domain = resolve_company_domain(company)
    assert domain == expected, f"{company} → {domain!r}, want {expected!r}"


@pytest.mark.parametrize(
    "company",
    [
        "Rochester Institute of Technology",
        "University of Rochester",
        "Rochester Regional Health",
        "Some Random Edu Lab",
    ],
)
def test_roche_not_confused_with_rochester(company):
    domain, conf = resolve_company_domain_with_confidence(company)
    assert domain != "roche.com"
    if domain:
        assert not domain.endswith(".edu") or "rochester" in (domain or "")


def test_edu_domains_rejected_for_woodway_targets():
    """Enterprise ICP should not resolve to .edu for pharma names."""
    for company in ("Roche", "Pfizer Inc", "Novartis AG"):
        domain = resolve_company_domain(company)
        assert domain
        assert not domain.endswith(".edu"), f"{company} wrongly resolved to {domain}"


def test_confidence_higher_for_exact_alias():
    d1, c1 = resolve_company_domain_with_confidence("Roche")
    d2, c2 = resolve_company_domain_with_confidence("Rochester Widgets LLC")
    assert d1 == "roche.com"
    assert c1 >= c2
