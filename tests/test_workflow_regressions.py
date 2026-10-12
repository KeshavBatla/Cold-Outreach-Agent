from pathlib import Path

import pytest
from langgraph.graph import END

import config
from agents.email_drafter import emailsender, run_emaildrafter
from agents.email_writer import run_emailwriter
from agents.cover_writer import run_coverwriter
from agents.manager import manager_router
from agents.researcher import run_researcher
from graph.workflow import create_initial_outreach_state
from tools import gmail, hunter, tavily_search


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{(tmp_path / 'history.sqlite3').as_posix()}")
    import persistence

    persistence.clear_engine_cache()
    persistence.get_engine()
    yield
    persistence.clear_engine_cache()


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "MOCK_MODE", True)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "MOCK_SCENARIO", "good")


def test_research_candidate_requires_explicit_langgraph_approval(isolated_database, mock_mode):
    thread_id = "test_approval_research"
    state = {
        "company_name": "Acme",
        "candidate_profile": "Entry-level agentic AI engineer",
        "mode": "open_role",
        "owner_id": "local-test",
    }
    config_value = {"configurable": {"thread_id": thread_id}}

    waiting = run_researcher(state, config_value, decision_callback=lambda _: "pending")
    assert waiting["research_outcome"] == "awaiting_review"
    assert waiting["research_approved"] is False

    approved = run_researcher(state, config_value, decision_callback=lambda _: "approve")
    assert approved["research_outcome"] == "approved"
    assert approved["research_approved"] is True


def test_research_candidate_rejection_terminates_and_is_not_approval(isolated_database, mock_mode):
    thread_id = "test_rejection_research"
    state = {
        "company_name": "Rejected Co",
        "candidate_profile": "Entry-level agentic AI engineer",
        "mode": "open_role",
        "owner_id": "local-test",
    }
    config_value = {"configurable": {"thread_id": thread_id}}

    run_researcher(state, config_value, decision_callback=lambda _: "pending")
    rejected = run_researcher(state, config_value, decision_callback=lambda _: "reject")

    assert rejected["research_outcome"] == "rejected"
    assert rejected["research_approved"] is False
    assert manager_router({**rejected, "cover_letter_approved": False}) == END


def test_role_mismatch_retries_within_limit_and_never_searches_hunter(
    isolated_database, mock_mode, monkeypatch
):
    monkeypatch.setattr(config, "MOCK_SCENARIO", "role_mismatch")
    hunter_calls = []
    monkeypatch.setattr(hunter, "search_domain_contacts", lambda *args, **kwargs: hunter_calls.append(args))
    monkeypatch.setattr(hunter, "verify_email", lambda *args, **kwargs: hunter_calls.append(args))
    result = run_researcher(
        {
            "candidate_profile": "Intern interested in LLM systems",
            "mode": "speculative",
            "max_retries": 2,
            "owner_id": "local-test",
        },
        {"configurable": {"thread_id": "test_role_mismatch"}},
        decision_callback=lambda _: "pending",
    )

    assert result["research_outcome"] == "no_matching_role"
    assert result["retry_count"] == 2
    assert hunter_calls == []


def test_no_contacts_and_provider_error_have_explicit_outcomes(isolated_database, mock_mode, monkeypatch):
    monkeypatch.setattr(config, "MOCK_SCENARIO", "no_contacts")
    no_contacts = run_researcher(
        {"company_name": "No Contacts Co", "mode": "open_role", "owner_id": "local-test"},
        {"configurable": {"thread_id": "test_no_contacts"}},
        decision_callback=lambda _: "pending",
    )
    assert no_contacts["research_outcome"] == "no_usable_contact"

    monkeypatch.setattr(config, "MOCK_SCENARIO", "provider_timeout")
    failed = run_researcher(
        {"company_name": "Timeout Co", "mode": "open_role", "owner_id": "local-test"},
        {"configurable": {"thread_id": "test_provider_timeout"}},
        decision_callback=lambda _: "pending",
    )
    assert failed["research_outcome"] == "failed"
    assert "timeout" in failed["research_error"].lower()


def test_mock_mode_end_to_end_has_no_live_provider_calls(
    isolated_database, mock_mode, monkeypatch, tmp_path
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live provider boundary must not be invoked in MOCK_MODE.")

    monkeypatch.setattr(tavily_search, "get_tavily_tool", forbidden)
    monkeypatch.setattr(hunter.requests, "get", forbidden)
    monkeypatch.setattr(gmail, "get_gmail_service", forbidden)
    monkeypatch.setattr(config, "get_llm", forbidden)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "configured-but-not-used")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "configured-but-not-used")
    monkeypatch.setattr(config, "HUNTER_API_KEY", "configured-but-not-used")

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"mock attachment")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    thread_id = "test_mock_e2e"
    cfg = {"configurable": {"thread_id": thread_id}}
    state = create_initial_outreach_state(
        company_name="Mock AI Labs",
        mode="open_role",
        raw_cv_text="Candidate experience with Python, AI agents, and LLM applications.",
        candidate_profile="Entry-level AI engineer",
        candidate_name="Mock Candidate",
        resume_path=str(resume),
        run_dir=str(run_dir),
        target_seniority="Intern",
        target_domain_keywords="agentic AI",
        location_preference="Remote",
    )

    researched = run_researcher(state, cfg, decision_callback=lambda _: "pending")
    assert researched["research_outcome"] == "awaiting_review"
    approved = run_researcher({**state, **researched}, cfg, decision_callback=lambda _: "approve")
    state.update(approved)
    assert state["research_approved"] is True

    state.update(run_coverwriter(state, cfg))
    assert state["cover_letter_approved"] is True
    state.update(run_emailwriter(state, cfg))
    assert state["email_approved"] is True

    draft = run_emaildrafter(state, cfg, decision_callback=lambda _: "pending")
    state.update(draft)
    assert state["delivery_status"] == "draft_ready"
    state["subject"] = "Reviewed subject from UI"
    state["email_body"] = "The exact body approved by the user."
    sent_args = []
    safe_send = gmail.send_email

    def capture_send(**kwargs):
        sent_args.append(kwargs)
        return safe_send(**kwargs)

    monkeypatch.setattr("agents.email_drafter.send_email", capture_send)
    final = run_emaildrafter(state, cfg, decision_callback=lambda _: "approve")
    assert final["final_email_sent"] is False
    assert final["delivery_status"] == "simulated"
    assert sent_args[0]["subject"] == "Reviewed subject from UI"
    assert sent_args[0]["body"] == "The exact body approved by the user."


def test_dry_run_does_not_call_gmail_send(monkeypatch):
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(gmail, "get_gmail_service", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Gmail must not be called")))
    result = gmail.send_email(
        "test@example.com",
        "Reviewed subject",
        "Reviewed body",
        attachment_paths=[],
        approved=True,
    )
    assert result["delivery_status"] == "dry_run"
    assert result["sent"] is False


@pytest.mark.parametrize(
    ("mock_execute", "expected_status", "expected_sent"),
    [
        (lambda: {"id": "gmail-id"}, "sent", True),
        (lambda: (_ for _ in ()).throw(RuntimeError("network disconnected")), "unknown", False),
        (lambda: {}, "unknown", False),
    ],
)
def test_gmail_send_outcomes_are_not_success_shaped(
    monkeypatch, mock_execute, expected_status, expected_sent
):
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(gmail, "ensure_external_call_allowed", lambda _run_id: None)
    monkeypatch.setattr(gmail, "record_provider_usage", lambda **kwargs: None)

    class Request:
        def execute(self):
            return mock_execute()

    class Messages:
        def send(self, **kwargs):
            return Request()

    class Users:
        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    monkeypatch.setattr(gmail, "get_gmail_service", lambda: Service())
    result = gmail.send_email(
        "test@example.com",
        "Reviewed subject",
        "Reviewed body",
        attachment_paths=[],
        approved=True,
    )
    assert result.get("sent", False) is expected_sent
    assert result["delivery_status"] == expected_status


def test_sender_requires_explicit_approval_before_dispatch():
    with pytest.raises(PermissionError, match="explicit user approval"):
        emailsender({
            "company_name": "Acme",
            "job_role": "Engineer",
            "contact_email": "person@acme.example",
            "email_body": "Hello",
            "subject": "Application",
            "delivery_status": "draft_ready",
        })


def test_parent_graph_resume_does_not_send_twice(monkeypatch):
    monkeypatch.setattr(
        "agents.email_drafter.send_email",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("A previously sent message must not be sent again.")),
    )
    result = emailsender({
        "company_name": "Acme",
        "job_role": "Engineer",
        "contact_email": "person@acme.example",
        "email_body": "Reviewed body",
        "subject": "Reviewed subject",
        "human_decision": "approve",
        "delivery_status": "sent",
        "sent": True,
    })
    assert result["sent"] is True
    assert result["delivery_status"] == "sent"
