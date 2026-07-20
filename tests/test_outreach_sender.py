"""Tests for outreach sender name helpers."""

from src.outreach import finalize_outreach_body, outreach_sender_name, outreach_sign_off


def test_outreach_sender_name_from_env(monkeypatch):
    monkeypatch.setenv("OUTREACH_SENDER_NAME", "Alex Demo")
    assert outreach_sender_name() == "Alex Demo"


def test_finalize_outreach_body_replaces_placeholder(monkeypatch):
    monkeypatch.setenv("OUTREACH_SENDER_NAME", "Alex Demo")
    body = "Hi there,\n\nBest,\n[Your name]\nAcme Corp"
    assert finalize_outreach_body(body) == "Hi there,\n\nBest,\nAlex Demo\nAcme Corp"


def test_outreach_sign_off():
    assert outreach_sign_off("Woodway Assurance").endswith("Woodway Assurance")
