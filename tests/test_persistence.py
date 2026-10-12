from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
import persistence


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'outreach.sqlite3').as_posix()}"
    monkeypatch.setattr(config, "DATABASE_URL", database_url)
    monkeypatch.setattr(config, "DUPLICATE_COOLDOWN_DAYS", 30)
    monkeypatch.setattr(config, "LOCAL_OWNER_ID", "local")
    persistence.clear_engine_cache()
    persistence.get_engine()
    yield
    persistence.clear_engine_cache()


def _record_sent(company, email, domain=None):
    token, blocked = persistence.reserve_outreach(
        f"run-{company}",
        company,
        domain,
        email,
        owner_id="owner-a",
    )
    assert blocked is None
    assert token
    persistence.mark_outreach_sending(token)
    persistence.complete_outreach(token, "sent", message_id="mock-provider-id")


def test_normalizers_preserve_distinct_similar_companies():
    assert persistence.normalize_company_name("  ACME, Inc. ") == "acme"
    assert persistence.normalize_company_name("Acme Labs") != persistence.normalize_company_name("Acme Laboratory")
    assert persistence.normalize_domain("HTTPS://WWW.Acme.Example/jobs") == "acme.example"
    assert persistence.normalize_email(" Person@Acme.Example ") == "person@acme.example"


def test_sent_history_survives_engine_restart(isolated_database):
    _record_sent("Acme Inc.", "recruiter@acme.example", "www.acme.example")
    persistence.clear_engine_cache()

    assert persistence.outreach_block_reason("Acme", "https://acme.example", "other@acme.example") == (
        "Recently contacted through this service."
    )


def test_similar_company_names_do_not_collide(isolated_database):
    _record_sent("Acme Labs", "recruiter@acmelabs.example")

    assert persistence.outreach_block_reason(
        "Acme Laboratory", "laboratory.example", "other@laboratory.example"
    ) is None


def test_same_company_name_with_a_different_domain_does_not_collide(isolated_database):
    _record_sent("Acme Inc.", "first@acme.example", "acme.example")

    assert persistence.outreach_block_reason(
        "Acme Inc.", "acme-industries.example", "second@acme-industries.example"
    ) is None


def test_company_and_recipient_formatting_normalize_for_duplicate_checks(isolated_database):
    _record_sent("Acme, Inc.", "Recruiter@Acme.Example")

    assert persistence.outreach_block_reason(
        " ACME INC ", "https://www.acme.example/jobs", "another@acme.example"
    ) == "Recently contacted through this service."
    assert persistence.outreach_block_reason(
        "Different Company", "different.example", " recruiter@ACME.example "
    ) == "Recently contacted through this service."


def test_rejected_research_and_failed_delivery_are_not_successful_sends(isolated_database):
    persistence.record_research_run("rejected-run", "No Thanks Co", None, "rejected", owner_id="owner-a")
    assert persistence.outreach_block_reason("No Thanks Co", None, "person@nothanks.example") is None

    token, blocked = persistence.reserve_outreach(
        "failed-run", "Failed Co", "failed.example", "person@failed.example", owner_id="owner-a"
    )
    assert blocked is None
    persistence.complete_outreach(token, "failed", error="provider rejected request")
    assert persistence.outreach_block_reason(
        "Failed Co", "failed.example", "person@failed.example"
    ) is None


def test_owner_exclusions_are_isolated(isolated_database):
    persistence.add_exclusion("owner-a", "Excluded Co", "excluded.example", "not a fit")

    assert persistence.company_block_reason(
        "Excluded Co", "excluded.example", owner_id="owner-a"
    ) == "This company is excluded for this owner."
    assert persistence.company_block_reason(
        "Excluded Co", "excluded.example", owner_id="owner-b"
    ) is None


def test_domain_exclusion_does_not_block_same_named_different_company(isolated_database):
    persistence.add_exclusion("owner-a", "Acme Inc.", "acme.example", "not a fit")

    assert persistence.company_block_reason(
        "Acme Inc.", "acme.example", owner_id="owner-a"
    ) == "This company is excluded for this owner."
    assert persistence.company_block_reason(
        "Acme Inc.", "acme-industries.example", owner_id="owner-a"
    ) is None


def test_cached_contacts_avoid_hunter_and_stale_verification_is_not_trusted(
    isolated_database, monkeypatch
):
    persistence.save_contacts(
        "Cached Co",
        "cached.example",
        [{"name": "Pat Example", "email": "pat@cached.example", "position": "Recruiter"}],
        verification_status="valid",
        owner_id="owner-a",
    )
    with Session(persistence.get_engine()) as session, session.begin():
        contact = session.scalar(select(persistence.Contact))
        contact.last_verified_at = persistence.utc_now() - timedelta(days=90)

    cached = persistence.get_cached_contacts(
        "Cached Co", "cached.example", owner_id="owner-a"
    )
    assert cached[0]["verification_status"] == "stale"

    def unexpected_hunter_call(*args, **kwargs):
        raise AssertionError("A fresh cached discovery result should avoid Hunter.")

    monkeypatch.setattr("agents.researcher.search_domain_contacts", unexpected_hunter_call)
    import config as app_config
    from agents.researcher import huntersearch

    monkeypatch.setattr(app_config, "MOCK_MODE", False)
    result = huntersearch({
        "company_name": "Cached Co",
        "company_domain": "cached.example",
        "owner_id": "owner-a",
        "research_run_id": "cache-test",
    })
    assert result["hunter_candidates"][0]["email"] == "pat@cached.example"
    assert result["hunter_candidates"][0]["verification_status"] == "stale"


def test_contact_cache_is_private_to_owner(isolated_database):
    persistence.save_contacts(
        "Private Contacts Co",
        "private.example",
        [{"name": "Private Person", "email": "person@private.example"}],
        owner_id="owner-a",
    )

    assert persistence.get_cached_contacts(
        "Private Contacts Co", "private.example", owner_id="owner-a"
    )
    assert persistence.get_cached_contacts(
        "Private Contacts Co", "private.example", owner_id="owner-b"
    ) == []


def test_contact_cache_does_not_cross_company_domains(isolated_database):
    persistence.save_contacts(
        "Acme Inc.",
        "acme.example",
        [{"name": "Private Person", "email": "person@acme.example"}],
        owner_id="owner-a",
    )

    assert persistence.get_cached_contacts(
        "Acme Inc.", "acme-industries.example", owner_id="owner-a"
    ) == []


def test_concurrent_reservations_cannot_both_authorize_duplicate(isolated_database):
    def reserve(index):
        return persistence.reserve_outreach(
            f"concurrent-{index}",
            "Concurrent Co",
            "concurrent.example",
            "same@concurrent.example",
            owner_id=f"owner-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, range(2)))

    assert sum(token is not None for token, _ in results) == 1
    assert sum(reason is not None for _, reason in results) == 1
