"""Tests for eval harness, account briefs, evidence-bound citations."""

from src.account_brief import assemble_account_brief, format_brief_text, get_account_brief
from src.evidence_bound import validate_citations
from src.outreach_eval import deterministic_checks, pairwise_prefer, run_eval_set, score_draft_rubric


def test_eval_set_heuristic_separates_good_and_bad():
    summary = run_eval_set("woodway", use_llm=False)
    assert summary["n"] >= 3
    by_id = {r["id"]: r for r in summary["results"]}
    assert by_id["bad_generic_fluent"]["mean"] < 3.5
    assert by_id["bad_generic_fluent"]["pass"] is True
    assert by_id["good_signal_specific"]["mean"] >= 3.5
    assert by_id["good_signal_specific"]["pass"] is True


def test_deterministic_bans_hope_this():
    r = deterministic_checks("Subject: hi\n\nHi,\nI hope this finds you well. Interested?\n")
    assert r["ok"] is False
    assert any("banned" in i for i in r["issues"])


def test_pairwise_prefers_clean_draft():
    good = "Subject: re-id on shared cohorts\n\nHi Sam,\nYour RWE partnership raises re-id risk on anonymized cohorts. Worth a look at how you evidence it today?\n\nBest"
    bad = "Subject: exciting opportunity to revolutionize\n\nHi,\nI hope this finds you well! Cutting-edge synergy awaits.\nLooking forward!"
    r = pairwise_prefer(good, bad, use_llm=False)
    assert r["winner"] == "A"


def test_validate_citations_rejects_unknown_proof(monkeypatch):
    monkeypatch.setenv("OUTREACH_REQUIRE_CITATIONS", "true")
    body = "EviData delivers 90% ROI for every customer including Acme Corp."
    cites = [{"sentence": body, "source": "proof:not_real"}]
    v = validate_citations(body, cites, agent="woodway")
    assert v["ok"] is False


def test_validate_citations_accepts_proof_core(monkeypatch):
    monkeypatch.setenv("OUTREACH_REQUIRE_CITATIONS", "true")
    sent = "EviData quantifies re-identification risk in anonymized datasets."
    body = f"Hi,\n\n{sent} Worth a look?\n\nBest"
    cites = [{"sentence": sent, "source": "proof:evidata_core"}]
    v = validate_citations(body, cites, agent="woodway", allowed_extra={"proof:evidata_core", "evidata_core"})
    assert v["ok"] is True


def test_account_brief_persists(tmp_db, monkeypatch):
    monkeypatch.setenv("ACCOUNT_BRIEFS_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT_BRIEF_FILINGS", "false")
    # Stub privacy fetch to avoid network
    monkeypatch.setattr(
        "src.privacy_footprint.fetch_privacy_footprint",
        lambda company, domain=None: {
            "mentions_deidentification": True,
            "dpo_name": "Ada DPO",
            "policy_url": "https://example.com/privacy",
        },
    )
    brief = assemble_account_brief("Pfizer", agent="woodway", domain="pfizer.com", force=True)
    assert "ACCOUNT BRIEF" in brief["brief_text"]
    assert "de-id mentioned=yes" in brief["brief_text"]
    cached = get_account_brief("Pfizer", agent="woodway")
    assert cached is not None
    assert cached["company"] == "Pfizer"


def test_format_brief_includes_signals():
    text = format_brief_text({
        "company": "Acme",
        "domain": "acme.com",
        "signals": [{"id": 1, "label": "CPO hire", "snippet": "appointed chief privacy"}],
        "privacy": {},
        "lead_facts": [],
        "filings": [],
    })
    assert "CPO hire" in text
    assert "[1]" in text


def test_score_draft_rubric_heuristic():
    body = open.__doc__  # noqa — just need any text
    body = (
        "Subject: re-id risk\n\nHi Jane,\n"
        "Your data-sharing partnership makes re-identification risk concrete. "
        "EviData quantifies it for governance reviews. Worth a 15-minute look?\n\nBest"
    )
    r = score_draft_rubric(body, subject="re-id risk", context="data-sharing partnership", use_llm=False)
    assert r["mean"] >= 3
    assert r["mode"] == "heuristic"
