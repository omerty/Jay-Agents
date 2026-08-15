"""Tests for Woodway pipeline best-path upgrades."""

from src.contact_tier import classify_contact_tier, tier_allows_email_draft
from src.email_verify import apply_verification_to_lead, mx_records_exist
from src.funnel_metrics import compute_funnel_from_summary
from src.outcomes import apply_learning_boost, get_learning_weights
from src.query_learning import rank_query_pool, score_query
from src.reply_classify import classify_reply_text
from src.email_patterns import resolve_company_domain_with_confidence, domain_confidence_ok


def test_unknown_email_is_tier_b():
    assert classify_contact_tier({
        "contact_name": "Jane Doe",
        "email": "jane@acme.com",
    }) == "B"


def test_verified_hunter_is_tier_a():
    assert classify_contact_tier({
        "contact_name": "Jane Doe",
        "email": "jane@acme.com",
        "email_verified": 1,
        "email_source": "hunter_verify",
    }) == "A"


def test_privacy_dpo_is_tier_b():
    assert classify_contact_tier({
        "contact_name": "Sam Lee",
        "email": "privacy@acme.com",
        "email_source": "privacy_dpo",
        "email_verified": 0,
    }) == "B"


def test_bounce_classification():
    r = classify_reply_text("Mail Delivery Failed: undeliverable mailbox unavailable 550", use_llm=False)
    assert r["class"] == "bounce"
    assert r.get("suppress")


def test_ooo_revive_date():
    r = classify_reply_text("I am out of office until March 15. Back then.", use_llm=False)
    assert r["class"] == "ooo"
    assert r.get("revive_at")


def test_negative_does_not_require_domain_suppress():
    r = classify_reply_text("Please unsubscribe me from this list", use_llm=False)
    assert r["class"] == "negative"
    assert r.get("suppress_email_only") is True


def test_funnel_rates():
    m = compute_funnel_from_summary({
        "agent": "woodway",
        "run_id": "abc",
        "steps": {
            "discovery": {"imported": 10, "companies": 10},
            "digest": {"kept": 5, "companies": ["a", "b", "c", "d", "e"]},
            "contacts": {"imported": 3, "updated": 1, "with_email": 2},
            "process": {"count": 3},
            "drafts": {"created": 2},
        },
    })
    assert m["kept"] == 5
    assert m["rates"]["keep_to_contact"] == 0.8


def test_query_rank_prefers_winning_industry():
    scored_pharma = score_query('pharma "chief privacy officer"', {"pharma": 1.2, "retail": 0.8})
    scored_retail = score_query('retail "chief privacy officer"', {"pharma": 1.2, "retail": 0.8})
    assert scored_pharma > scored_retail


def test_rank_query_pool_stable(monkeypatch):
    monkeypatch.setattr(
        "src.query_learning.get_learning_weights",
        lambda agent="woodway": {
            "industry_weights": {"pharma": 1.3},
            "title_weights": {"chief privacy": 1.2},
        },
    )
    pool = [
        'bank "data governance"',
        'pharma "chief privacy officer"',
        "random vendor saas",
    ]
    ranked = rank_query_pool(pool, agent="woodway")
    assert ranked[0].startswith("pharma")


def test_domain_override_high_confidence():
    domain, conf = resolve_company_domain_with_confidence("JPMorgan Chase")
    assert domain == "jpmorganchase.com"
    assert domain_confidence_ok(conf)


def test_verify_patch_invalid():
    patch = apply_verification_to_lead(
        {"email": "x@y.com", "email_inferred": 1},
        {"ok": False, "status": "invalid"},
    )
    assert patch["email_verified"] == 0
    assert patch["contact_tier"] == "B"


def test_learning_boost_noop_without_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "t.db"))
    import importlib
    import src.db as db
    importlib.reload(db)
    db.init_db()
    # reload outcomes against new db path
    import src.outcomes as outcomes
    importlib.reload(outcomes)
    score, reasons = outcomes.apply_learning_boost(60, "pharma", "Chief Privacy Officer")
    assert score == 60
    assert reasons == []
