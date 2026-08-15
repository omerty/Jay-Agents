"""Prod-readiness regressions — domains, owners, memory, retention hooks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


DOMAIN_GOLDEN = [
    ("Roche", "roche.com"),
    ("Pfizer", "pfizer.com"),
    ("Novartis", "novartis.com"),
    ("Merck", "merck.com"),
    ("Sanofi", "sanofi.com"),
    ("Eli Lilly", "lilly.com"),
    ("Moderna", "modernatx.com"),
    ("AstraZeneca", "astrazeneca.com"),
    ("Bristol Myers Squibb", "bms.com"),
    ("Johnson & Johnson", "jnj.com"),
    ("Takeda", "takeda.com"),
    ("JPMorgan Chase", "jpmorganchase.com"),
    ("Citigroup", "citi.com"),
    ("Goldman Sachs", "gs.com"),
    ("Bank of America", "bankofamerica.com"),
    ("Wells Fargo", "wellsfargo.com"),
    ("Morgan Stanley", "morganstanley.com"),
    ("UnitedHealth", "unitedhealthgroup.com"),
    ("Prudential", "prudential.com"),
    ("Genentech", "gene.com"),
]


@pytest.mark.parametrize("company,expected", DOMAIN_GOLDEN)
def test_domain_overrides_golden(company, expected):
    from src.email_patterns import resolve_company_domain, resolve_company_domain_with_confidence

    domain, conf = resolve_company_domain_with_confidence(company)
    assert domain == expected, f"{company} → {domain} (want {expected})"
    assert conf >= 0.9
    assert resolve_company_domain(company) == expected


def test_roche_never_rochester(monkeypatch):
    """Clearbit-style wrong hit must not win over override / edu rejection."""
    from src import email_patterns

    monkeypatch.setattr(
        email_patterns,
        "enrich_company",
        lambda company: {"domain": "rochester.edu", "name": "University of Rochester"},
    )
    domain, conf = email_patterns.resolve_company_domain_with_confidence("Roche")
    assert domain == "roche.com"
    assert conf >= 0.9


def test_slug_token_rejects_roche_in_rochester():
    from src.email_patterns import _slug_token_hit

    assert not _slug_token_hit("roche", "rochester")
    assert _slug_token_hit("moderna", "modernatx")
    assert _slug_token_hit("pfizer", "pfizer")


def test_edu_domain_rejected_without_override(monkeypatch):
    from src import email_patterns

    monkeypatch.setattr(
        email_patterns,
        "enrich_company",
        lambda company: {"domain": "mit.edu"},
    )
    domain, conf = email_patterns.resolve_company_domain_with_confidence("Acme Widgets Inc")
    assert domain is None
    assert conf == 0.0


def test_regional_exec_rejected():
    from src.owner_resolution import classify_owner_title

    cases = [
        ("Regional President", "Some Co"),
        ("VP, Eastern Canada", "National Brand Inc"),
        ("President, Canada", "Siemens Canada"),
        ("General Manager Ontario", "Local Ops"),
        ("CEO", "Coca-Cola Canada"),
        ("President", "Walmart Canada"),
    ]
    for title, company in cases:
        out = classify_owner_title(title, company=company)
        assert out["reject"] or out["ownership_confidence"] < 0.5, f"{title} @ {company}: {out}"


def test_true_owner_accepted():
    from src.owner_resolution import classify_owner_title

    out = classify_owner_title("Owner & President", company="Ottawa Metal Works")
    assert out["is_owner"]
    assert not out["reject"]
    assert out["ownership_confidence"] >= 0.85


def test_memory_extend_on_repeat_reject(tmp_db, monkeypatch):
    monkeypatch.setenv("KEIRA_RESEARCH_MEMORY", "true")
    monkeypatch.setenv("KEIRA_RESEARCH_COOLDOWN_DAYS", "90")
    from src.keira_memory import get_memory, remember_research
    from datetime import datetime

    first = remember_research("Thin Co", status="rejected", reason="thin evidence")
    second = remember_research("Thin Co", status="rejected", reason="thin evidence again")
    mem = get_memory("Thin Co")
    assert mem
    meta = __import__("json").loads(mem["meta_json"] or "{}")
    assert meta.get("reject_streak", 0) >= 1
    # Second next_research_at should be later than first's base
    t1 = datetime.fromisoformat(first["next_research_at"].replace("Z", "+00:00"))
    t2 = datetime.fromisoformat(second["next_research_at"].replace("Z", "+00:00"))
    assert t2 >= t1


def test_memory_research_later_shorter_than_reject(tmp_db, monkeypatch):
    monkeypatch.setenv("KEIRA_RESEARCH_MEMORY", "true")
    from src.keira_memory import remember_research
    from datetime import datetime, timezone

    later = remember_research("Maybe Co", status="research_later", reason="thin")
    rej = remember_research("Bad Co", status="rejected", reason="broker")
    t_later = datetime.fromisoformat(later["next_research_at"].replace("Z", "+00:00"))
    t_rej = datetime.fromisoformat(rej["next_research_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert (t_later - now).days <= 45
    assert (t_rej - now).days >= 179  # ~180d; allow off-by-one on partial days


def test_purge_stale_contacts(tmp_db, monkeypatch):
    monkeypatch.setenv("RETENTION_MONTHS", "1")
    from src.compliance import purge_stale_contacts
    from src.db import get_connection, init_db, upsert_lead

    init_db()
    lid, _ = upsert_lead(
        {"company": "Old Co", "status": "discovered", "source": "discover"},
        agent="woodway",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with get_connection() as conn:
        conn.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (old, lid))
        conn.commit()
    n = purge_stale_contacts(months=1)
    assert n >= 1
