"""
Tests for Hunter contact scoring and verification.
"""

import pytest
from tools.hunter import score_contact, verify_email


def test_score_contact_director_of_sales_not_in_top_tier():
    # "Director of Sales" should NOT match high priority (+100) or medium priority (+50)
    contact = {"position": "Director of Sales", "department": "Sales"}
    score = score_contact(contact)
    assert score < 100, f"Expected Director of Sales score < 100, got {score}"


def test_score_contact_cto_top_tier():
    contact = {"position": "CTO", "department": "Executive"}
    # High priority gives +100
    assert score_contact(contact) >= 100


def test_score_contact_co_founder_and_cto_top_tier():
    contact = {"position": "Co-Founder & CTO", "department": "Executive"}
    assert score_contact(contact) >= 100


def test_score_contact_director_of_engineering_top_tier():
    contact = {"position": "Director of Engineering", "department": "Engineering"}
    assert score_contact(contact) >= 100


def test_score_contact_head_of_engineering_top_tier():
    contact = {"position": "Head of Engineering", "department": "Engineering"}
    assert score_contact(contact) >= 100


def test_score_contact_technical_recruiter():
    # Only position gives recruiter_titles (+80)
    contact = {"position": "Technical Recruiter", "department": "Other"}
    assert score_contact(contact) == 80


def test_verify_email_no_key_behavior(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("ALLOW_UNVERIFIED_EMAILS", raising=False)

    import config
    monkeypatch.setattr(config, "HUNTER_API_KEY", None)

    # Without ALLOW_UNVERIFIED_EMAILS=true, should return False
    assert verify_email("test@example.com", api_key=None) is False

    # With ALLOW_UNVERIFIED_EMAILS=true, should return True
    monkeypatch.setenv("ALLOW_UNVERIFIED_EMAILS", "true")
    assert verify_email("test@example.com", api_key=None) is True

