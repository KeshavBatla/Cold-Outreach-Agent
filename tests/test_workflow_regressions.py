from pathlib import Path

import pytest
from langgraph.graph import END

import config
from agents import researcher
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
            "search_budget": 2,
            "owner_id": "local-test",
        },
        {"configurable": {"thread_id": "test_role_mismatch"}},
        decision_callback=lambda _: "pending",
    )

    assert result["research_outcome"] == "search_exhausted"
    assert result["retry_count"] == 2
    assert hunter_calls == []


def _run_mocked_candidate_search(monkeypatch, candidates, *, mode="speculative", budget=3, seed_query=None):
    searches = []

    def query(state):
        attempt = state.get("search_attempt", 0) + 1
        return {"search_query": str(attempt), "search_attempt": attempt}

    def cache(_state):
        return {"research_cache_hit": False}

    def tavily(state):
        searches.append(state["search_attempt"])
        return {"tavily_findings": str(state["search_attempt"])}

    def extract(state):
        candidate = candidates[state["search_attempt"] - 1]
        return {
            key: value
            for key, value in candidate.items()
            if key != "suitable_attempts"
        }

    def evaluate(state):
        suitable = state["search_attempt"] in candidates[0].get("suitable_attempts", [])
        return {
            "research_sufficient": suitable,
            "research_outcome": "candidate_found" if suitable else "no_matching_role",
            "evaluator_feedback": (
                "Role satisfies the candidate requirements."
                if suitable
                else "Role does not satisfy the seniority or domain requirements."
            ),
            "retry_count": state.get("retry_count", 0) + (0 if suitable else 1),
            "retry_counted": not suitable,
        }

    def contacts(_state):
        return {
            "hunter_candidates": [{"name": "Recruiter", "email": "recruiter@example.test"}],
            "hunter_candidate_index": 0,
            "contact_attempts": 0,
        }

    def verified(state):
        return {
            "contact_verified": True,
            "contact_attempts": 1,
            "contact_email": "recruiter@example.test",
            "research_outcome": "awaiting_review",
        }

    monkeypatch.setattr(researcher, "inputpromptformater", query)
    monkeypatch.setattr(researcher, "research_cache", cache)
    monkeypatch.setattr(researcher, "tavilysearch", tavily)
    monkeypatch.setattr(researcher, "formatingnode", extract)
    monkeypatch.setattr(researcher, "llm_evaluates", evaluate)
    monkeypatch.setattr(researcher, "huntersearch", contacts)
    monkeypatch.setattr(researcher, "verify_candidate", verified)
    graph = researcher.build_researcher_graph()
    graph_config = {"configurable": {"thread_id": f"candidate-search-{id(candidates)}"}}
    graph.invoke(
        {
            "messages": [],
            "candidate_profile": "Entry-level agentic AI engineer",
            "mode": mode,
            "seed_query": seed_query,
            "retry_count": 0,
            "max_retries": budget,
            "search_attempt": 0,
            "search_budget": budget,
            "retry_counted": False,
            "rejected_candidates": [],
            "owner_id": "test-owner",
            "research_run_id": "test-run",
            "contact_attempts": 0,
            "research_outcome": None,
        },
        graph_config,
    )
    return graph, graph_config, searches


def test_speculative_search_skips_unsuitable_candidate_and_pauses_on_next_suitable(
    monkeypatch,
):
    candidates = [
        {
            "company_name": "LangChain",
            "company_domain": "langchain.com",
            "job_role": "Senior Sales Director",
            "job_description": "Senior commercial role.",
            "research_summary": "LangChain role does not fit candidate seniority.",
            "suitable_attempts": [2],
        },
        {
            "company_name": "Agentic Labs",
            "company_domain": "agentic.example",
            "job_role": "AI Engineer Intern",
            "job_description": "Build agentic AI systems.",
            "research_summary": "Intern role working on LLM agents.",
            "suitable_attempts": [2],
        },
    ]
    graph, graph_config, searches = _run_mocked_candidate_search(
        monkeypatch, candidates, budget=3
    )
    snapshot = graph.get_state(graph_config)

    assert searches == [1, 2]
    assert snapshot.next == ("human_evaluation",)
    assert snapshot.values["research_outcome"] == "awaiting_review"
    assert snapshot.values["company_name"] == "Agentic Labs"
    assert snapshot.values["rejected_candidates"] == [{
        "status": "rejected",
        "company_name": "LangChain",
        "company_domain": "langchain.com",
        "job_role": "Senior Sales Director",
        "reason": "Role does not satisfy the seniority or domain requirements.",
    }]


def test_speculative_search_budget_exhausts_without_repeating_or_infinite_loop(monkeypatch):
    candidate = {
        "company_name": "LangChain",
        "company_domain": "langchain.com",
        "job_role": "Senior Sales Director",
        "job_description": "Senior commercial role.",
        "research_summary": "Not a suitable role.",
        "suitable_attempts": [],
    }
    graph, graph_config, searches = _run_mocked_candidate_search(
        monkeypatch, [candidate, candidate], budget=2
    )
    snapshot = graph.get_state(graph_config)

    assert searches == [1, 2]
    assert snapshot.next == ()
    assert snapshot.values["research_outcome"] == "search_exhausted"
    assert len(snapshot.values["rejected_candidates"]) == 1


def test_named_company_search_does_not_switch_companies(monkeypatch):
    candidates = [
        {
            "company_name": "Requested Co",
            "company_domain": "requested.example",
            "job_role": "Senior Sales Director",
            "job_description": "Senior commercial role.",
            "research_summary": "No suitable role at requested company.",
            "suitable_attempts": [],
        },
        {
            "company_name": "Unrelated Co",
            "company_domain": "unrelated.example",
            "job_role": "AI Engineer Intern",
            "job_description": "Intern role.",
            "research_summary": "Unrelated result.",
            "suitable_attempts": [2],
        },
    ]
    graph, graph_config, searches = _run_mocked_candidate_search(
        monkeypatch,
        candidates,
        mode="open_role",
        budget=3,
        seed_query="Requested Co",
    )
    snapshot = graph.get_state(graph_config)

    assert searches == [1]
    assert snapshot.values["research_outcome"] == "no_matching_role"
    assert snapshot.values["company_name"] == "Requested Co"


def test_named_company_rejects_results_for_an_unrelated_company():
    result = researcher.llm_evaluates({
        "mode": "open_role",
        "seed_query": "Requested Co",
        "company_name": "Unrelated Co",
        "job_role": "AI Engineer Intern",
        "retry_count": 0,
    })
    assert result["research_outcome"] == "no_matching_role"
    assert result["research_sufficient"] is False


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
