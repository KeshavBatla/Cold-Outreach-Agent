from __future__ import annotations

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from persistence import (
    AppUser,
    ApplicationRun,
    Base,
    BatchItem,
    DraftVersion,
    Exclusion,
    OutboxMessage,
    PrivateArtifact,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.main import create_app
from webapp.settings import Settings
from webapp.worker import (
    _finish_research,
    _process_drafts,
    _process_research_decision,
)


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=tmp_path / "private",
        max_batch_items=10,
        max_draft_generations=3,
        max_research_attempts=4,
        test_principal_enabled=False,
        app_environment="test",
        google_oauth_client_id="test-client-id",
        google_oauth_client_secret="test-client-secret",
        google_oauth_redirect_uri="http://localhost:8000/api/v1/gmail/oauth/callback",
        gmail_test_mode=True,
        gmail_live_send_enabled=False,
    )
    app = create_app(settings=settings, sessions=sessions, readiness_check=lambda: True)
    owner = Principal(user_id="user-1", subject="sub-1", email="user@example.test")
    app.dependency_overrides[get_current_principal] = lambda: owner

    with sessions.begin() as session:
        session.add(AppUser(
            id=owner.user_id,
            identity_provider=owner.identity_provider,
            external_subject=owner.subject,
            email=owner.email,
        ))

    return {
        "app": app,
        "sessions": sessions,
        "settings": settings,
        "owner": owner,
        "tmp_path": tmp_path,
    }


def test_scenario_1_ineligible_result_marks_appropriate_and_no_draft(test_env):
    """Scenario 1 & 3: Ineligible result is marked appropriately; no draft is generated."""
    sessions = test_env["sessions"]
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id="batch-1",
            owner_id="user-1",
            mode="speculative",
            status="researching",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id="user-1",
            batch_id="batch-1",
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="running",
            phase="research",
            progress=20,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    # Finish research with an ineligible outcome
    values = {
        "company_name": "Ineligible Co",
        "research_outcome": "no_matching_role",
        "research_sufficient": False,
        "search_attempt": 1,
    }
    _finish_research(sessions, run_id, item_id, values, paused=False)

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "ineligible"
        assert item.failure_code == "no_matching_role"
        assert run.status == "completed"

        # Verify no draft exists
        drafts = session.scalars(select(DraftVersion).where(DraftVersion.item_id == item_id)).all()
        assert len(drafts) == 0


def test_scenario_2_user_rejection_in_speculative_mode_continues_search(test_env, monkeypatch):
    """Scenario 2 & 5: When user rejects result in speculative mode, search continues."""
    sessions = test_env["sessions"]
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id="batch-1",
            owner_id="user-1",
            mode="speculative",
            status="research_review",
            research_decision="rejected",
            research_result={
                "company_name": "Candidate A",
                "company_domain": "candidate-a.test",
                "job_role": "Engineer",
                "search_attempt": 1,
            },
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id="user-1",
            batch_id="batch-1",
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="paused",
            phase="research_review",
            progress=45,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    # Mock researcher graph so it doesn't run LLM
    class MockGraph:
        def get_state(self, config):
            class State:
                next = None
                values = {"human_decision": "reject"}
            return State()

        def update_state(self, config, values):
            pass

        def invoke(self, input_val, config):
            pass

    import webapp.worker as worker
    monkeypatch.setattr(worker, "build_researcher_graph", lambda checkpointer: MockGraph())

    payload = {"run_id": run_id, "item_id": item_id, "decision": "rejected"}
    _process_research_decision(sessions, payload, checkpointer=None)

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        # In speculative mode with attempt 1 < 4, it should re-enqueue research
        assert item.status == "queued"
        assert item.research_decision is None
        assert run.status == "queued"
        assert run.phase == "research"

        # Check outbox has continuation message
        outbox = session.scalars(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        ).all()
        assert len(outbox) == 1
        assert "research-continue" in outbox[0].dedupe_key

        # Check company was recorded in exclusion
        exclusion = session.scalars(select(Exclusion)).all()
        assert any("candidate-a" in ex.normalized_key for ex in exclusion)


def test_scenario_6_user_rejection_in_named_company_mode_does_not_continue(test_env, monkeypatch):
    """Scenario 6: In named-company mode, rejection respects the target and does not substitute."""
    sessions = test_env["sessions"]
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id="batch-1",
            owner_id="user-1",
            mode="open_role",
            target_company="Acme Corp",
            status="research_review",
            research_decision="rejected",
            research_result={
                "company_name": "Acme Corp",
                "company_domain": "acme.test",
                "job_role": "Backend Lead",
                "search_attempt": 1,
            },
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id="user-1",
            batch_id="batch-1",
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="paused",
            phase="research_review",
            progress=45,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    class MockGraph:
        def get_state(self, config):
            class State:
                next = None
                values = {"human_decision": "reject"}
            return State()

    import webapp.worker as worker
    monkeypatch.setattr(worker, "build_researcher_graph", lambda checkpointer: MockGraph())

    payload = {"run_id": run_id, "item_id": item_id, "decision": "rejected"}
    _process_research_decision(sessions, payload, checkpointer=None)

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        # In open_role mode, rejection completes the item without substituting
        assert item.status == "rejected"
        assert run.status == "completed"

        # No new research outbox message
        outbox = session.scalars(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        ).all()
        assert len(outbox) == 0


def test_scenario_7_8_max_research_attempts_reached_finishes_gracefully(test_env, monkeypatch):
    """Scenario 7 & 8: Search reaches max attempts and finishes with no_suitable_opportunity."""
    sessions = test_env["sessions"]
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id="batch-1",
            owner_id="user-1",
            mode="speculative",
            status="research_review",
            research_decision="rejected",
            research_result={
                "company_name": "Candidate Final",
                "company_domain": "final.test",
                "job_role": "Engineer",
                "search_attempt": 5,  # >= MAX_RESEARCH_ATTEMPTS (5)
            },
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id="user-1",
            batch_id="batch-1",
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="paused",
            phase="research_review",
            progress=45,
            attempt=5,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    class MockGraph:
        def get_state(self, config):
            class State:
                next = None
                values = {"human_decision": "reject"}
            return State()

    import webapp.worker as worker
    monkeypatch.setattr(worker, "build_researcher_graph", lambda checkpointer: MockGraph())

    payload = {"run_id": run_id, "item_id": item_id, "decision": "rejected"}
    _process_research_decision(sessions, payload, checkpointer=None)

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        # Max attempts reached: stops gracefully
        assert item.status in {"ineligible", "rejected"}
        assert run.status == "completed"


def test_scenario_draft_idempotency_avoids_duplicate_generation(test_env):
    """Issue 2: Draft generation idempotency prevents duplicate execution."""
    sessions = test_env["sessions"]
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id="batch-1",
            owner_id="user-1",
            mode="speculative",
            status="drafting",
            research_decision="approved",
            cv_artifact_id="test-cv",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id="user-1",
            batch_id="batch-1",
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="running",
            phase="drafting",
            progress=55,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        # Existing draft already created
        session.add(DraftVersion(
            id=str(uuid.uuid4()),
            owner_id="user-1",
            batch_id="batch-1",
            item_id=item_id,
            artifact_type="cover_letter",
            version=1,
            content="Already created cover letter",
            created_by="system",
            created_at=utc_now(),
        ))

    payload = {"run_id": run_id, "item_id": item_id}
    # Invoking _process_drafts should exit early without error due to idempotency
    _process_drafts(sessions, payload, checkpointer=None)

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "draft_review"
        assert run.status == "paused"
        assert run.phase == "draft_review"



def test_research_approval_to_draft_generation_end_to_end(test_env, monkeypatch):
    """Scenario: Approving research enqueues resume task and successfully generates both drafts."""
    from unittest.mock import MagicMock
    from evaluation.llm_evaluator import EmailSubject
    import agents.email_drafter as ed
    import config as app_config

    sessions = test_env["sessions"]
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    now = utc_now()

    # Create batch item with CV artifact
    with sessions.begin() as session:
        session.add(PrivateArtifact(
            id="test-cv-artifact",
            owner_id="user-1",
            batch_id=batch_id,
            item_id=item_id,
            artifact_type="cv",
            storage_key="user-1/cv.txt",
            filename="cv.txt",
            media_type="text/plain",
            byte_size=10,
            sha256="b" * 64,
            created_at=now,
        ))
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id="user-1",
            mode="open_role",
            status="research_review",
            research_decision=None,
            candidate_name="Jane Doe",
            cv_artifact_id="test-cv-artifact",
            research_result={
                "company_name": "Target Tech",
                "job_role": "Platform Engineer",
                "research_summary": "Pioneering cloud platform engineering.",
            },
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id="user-1",
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="paused",
            phase="research_review",
            progress=45,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))

    # Mock CV text loader
    import webapp.worker as worker
    monkeypatch.setattr(worker, "_private_text", lambda *_args: "Experienced Software and Platform Engineer with Python and Cloud.")
    monkeypatch.setattr(worker, "generate_candidate_profile", lambda _txt: "Senior Platform Engineer")

    # Mock graphs for cover writer and email writer
    class MockGraph:
        def __init__(self, output):
            self.output = output
        def get_state(self, config):
            class State:
                next = None
                values = {}
            return State()
        def invoke(self, input_val, config):
            return self.output

    monkeypatch.setattr(worker, "build_coverwriter_graph", lambda **_kwargs: MockGraph({"cover_letter": "Targeted Cover Letter"}))
    monkeypatch.setattr(worker, "build_emailwriter_graph", lambda **_kwargs: MockGraph({"email_body": "Personalized Email Body"}))

    # Test emaildrafter live code path (MOCK_MODE=False) with mocked LLM extractor
    monkeypatch.setattr(app_config, "MOCK_MODE", False)
    mock_extractor = MagicMock()
    mock_extractor.invoke.return_value = {"responses": [EmailSubject(subject="Platform Engineer Application — Jane Doe")]}
    monkeypatch.setattr(ed, "get_subject_generator", lambda _llm: mock_extractor)
    monkeypatch.setattr(app_config, "get_llm", lambda: MagicMock())

    # 1. Process research decision (approval)
    # Mock researcher graph so it doesn't run LLM
    class MockResearchGraph:
        def get_state(self, config):
            class State:
                next = None
                values = {"human_decision": "approve"}
            return State()
        def update_state(self, config, values):
            pass
        def invoke(self, input_val, config):
            pass

    monkeypatch.setattr(worker, "build_researcher_graph", lambda checkpointer: MockResearchGraph())

    with sessions.begin() as session:
        item = session.get(BatchItem, item_id)
        item.research_decision = "approved"

    worker._process_research_decision(sessions, {"run_id": run_id, "item_id": item_id, "decision": "approved"}, checkpointer=None)

    # Verify that drafting was queued
    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "drafting"
        assert run.phase == "drafting"
        outbox = session.scalars(select(OutboxMessage).where(OutboxMessage.topic == "draft_item")).all()
        assert len(outbox) == 1

    # 2. Process drafts
    worker._process_drafts(sessions, {"run_id": run_id, "item_id": item_id}, checkpointer=None)

    # Verify drafts persisted and item in draft_review
    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "draft_review"
        assert run.status == "paused"
        assert run.phase == "draft_review"
        assert run.progress == 90

        drafts = session.scalars(select(DraftVersion).where(DraftVersion.item_id == item_id)).all()
        assert len(drafts) == 2
        cl = next(d for d in drafts if d.artifact_type == "cover_letter")
        em = next(d for d in drafts if d.artifact_type == "email")
        assert cl.content == "Targeted Cover Letter"
        assert em.content == "Personalized Email Body"
        assert em.subject == "Platform Engineer Application — Jane Doe"


def test_ai_provider_exception_logs_and_marks_failure_cleanly(test_env, monkeypatch):
    """Scenario: AI provider exception during generation is logged and updates failure state."""
    sessions = test_env["sessions"]
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    now = utc_now()

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id="user-1",
            mode="open_role",
            status="drafting",
            research_decision="approved",
            cv_artifact_id="cv",
            research_result={"company_name": "Co", "job_role": "Eng"},
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id="user-1",
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="running",
            phase="drafting",
            progress=55,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))

    import webapp.worker as worker
    monkeypatch.setattr(worker, "_private_text", lambda *_args: "CV text")
    monkeypatch.setattr(worker, "generate_candidate_profile", lambda _txt: "Profile")

    def fail_cover(**_kwargs):
        raise RuntimeError("Google Generative AI ResourceExhausted: 429 Rate limit quota exceeded")

    monkeypatch.setattr(worker, "build_coverwriter_graph", fail_cover)

    outbox_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(OutboxMessage(
            id=outbox_id,
            topic="draft_item",
            dedupe_key="draft-fail-test",
            payload={"run_id": run_id, "item_id": item_id, "batch_id": batch_id, "owner_id": "user-1"},
            available_at=now,
            created_at=now,
        ))

    # Process outbox message through Celery task handler
    monkeypatch.setattr(worker, "_database_factory", lambda: (test_env["settings"], sessions))
    monkeypatch.setattr(worker, "postgres_checkpointer", lambda _url: None)

    class MockContextManager:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(worker, "postgres_checkpointer", lambda _url: MockContextManager())
    worker.process_outbox_message(outbox_id)

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "failed"
        assert item.failure_code == "ai_provider_quota_exceeded"
        assert run.status == "failed"
        assert run.safe_error_code == "ai_provider_quota_exceeded"
