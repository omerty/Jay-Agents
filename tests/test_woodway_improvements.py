"""Tests for Phase 1 Woodway improvements."""

import pytest

from src.contact_tier import classify_contact_tier, tier_allows_email_draft
from src.email_patterns import apply_pattern_to_name, normalize_domain
from src.negative_list import is_excluded


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/about") == "acme.com"


def test_apply_pattern_first_last():
    email = apply_pattern_to_name("{first}.{last}", "Jane", "Doe", "acme.com")
    assert email == "jane.doe@acme.com"


def test_contact_tier_verified():
    assert classify_contact_tier({
        "contact_name": "Jane Doe",
        "email": "jane@acme.com",
        "email_verified": 1,
        "email_source": "hunter_verify",
    }) == "A"


def test_contact_tier_inferred():
    assert classify_contact_tier({
        "contact_name": "Jane Doe",
        "email": "jane@acme.com",
        "email_inferred": 1,
    }) == "B"


def test_contact_tier_linkedin_only():
    assert classify_contact_tier({
        "contact_name": "Jane Doe",
        "linkedin_url": "https://linkedin.com/in/jane",
    }) == "C"


def test_tier_allows_email_draft():
    assert tier_allows_email_draft("A") is True
    assert tier_allows_email_draft("B") is True
    assert tier_allows_email_draft("C") is False


def test_negative_list_blocks_onetrust():
    assert is_excluded("OneTrust") == "blocklist"


def test_negative_list_allows_acme():
    assert is_excluded("Acme Health Systems") is None
