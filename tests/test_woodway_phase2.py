"""Tests for Phase 2-4 Woodway features."""

from src.evidence import filter_evidence_or_drop, flatten_for_record
from src.reply_classify import classify_reply_text
from src.signals import compute_priority, recency_decay


def test_evidence_or_drop():
    raw = {
        "company": {"value": "Acme", "src": "https://example.com", "snippet": "Acme Health"},
        "industry": {"value": "healthcare", "src": "https://example.com", "snippet": "hospital"},
        "signal": "no evidence",
        "employee_count": {"value": 5000, "src": "https://example.com", "snippet": "5000 employees"},
    }
    cleaned = filter_evidence_or_drop(raw, ["industry", "signal", "employee_count"])
    assert "industry" in cleaned
    assert "employee_count" in cleaned
    assert cleaned["employee_count"]["value"] == "5000"
    assert "signal" not in cleaned
    flat = flatten_for_record(cleaned)
    assert flat["industry"] == "healthcare"


def test_recency_decay():
    assert recency_decay(0, 90) == 1.0
    assert recency_decay(90, 90) == 0.5


def test_compute_priority():
    p = compute_priority(80, 2.0, 7, 90, "A")
    assert p > 100


def test_classify_ooo():
    r = classify_reply_text("I am out of office until Monday.", use_llm=False)
    assert r["class"] == "ooo"


def test_classify_negative():
    r = classify_reply_text("Please unsubscribe me", use_llm=False)
    assert r["class"] == "negative"
