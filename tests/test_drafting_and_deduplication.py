from __future__ import annotations

import re
import uuid
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
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
    ResearchCache,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.main import create_app
from webapp.settings import Settings
from webapp.worker import (
    _mark_failed,
    _process_drafts,
    _research_inputs,
    _safe_error_code,
)
import config as legacy_config
from agents.researcher import inputpromptformater, formatingnode, llm_evaluates, research_cache


@pytest.fixture
def env(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=artifact_dir,
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

    monkeypatch.setenv("PRIVATE_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setattr(legacy_config, "MOCK_MODE", True)
    monkeypatch.setattr(legacy_config, "MOCK_SCENARIO", "good")

    return {
        "app": app,
        "sessions": sessions,
        "settings": settings,
        "owner": owner,
        "client": TestClient(app),
        "artifact_dir": artifact_dir,
    }


def test_scenario_1_research_approval_to_draft_generation_and_persistence(env):
    """1. Approved research generates, incrementally persists, and returns cover letter and email drafts."""
    sessions = env["sessions"]
    client = env["client"]
    owner = env["owner"]

    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())

    artifact_file = env["artifact_dir"] / f"{artifact_id}.txt"
    artifact_file.write_text("Experienced Python & AI Engineer with 3 years building agentic workflows.", encoding="utf-8")

    with sessions.begin() as session:
        session.add(PrivateArtifact(
            id=artifact_id,
            owner_id=owner.user_id,
            artifact_type="cv",
            storage_key=f"{artifact_id}.txt",
            filename="John_Doe_CV.pdf",
            media_type="application/pdf",
            byte_size=100,
            created_at=utc_now(),
        ))
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode="speculative",
            candidate_name="John Doe",
            cv_artifact_id=artifact_id,
            status="research_review",
            research_decision="approved",
            research_result={
                "company_name": "Anthropic",
                "company_domain": "anthropic.com",
                "job_role": "AI Engineer",
                "job_description": "Building Claude agents",
                "research_summary": "Leading AI research lab focused on alignment",
                "contact_name": "Dario Amodei",
                "contact_email": "dario@anthropic.com",
            },
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="running",
            phase="drafting",
            progress=50,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    checkpointer = MemorySaver()
    _process_drafts(sessions, {"run_id": run_id, "item_id": item_id}, checkpointer=checkpointer)

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "draft_review"
        assert run.status == "paused"
        assert run.phase == "draft_review"
        assert run.progress == 90

        drafts = session.scalars(
            select(DraftVersion).where(DraftVersion.item_id == item_id)
        ).all()
        assert len(drafts) == 2
        types = {d.artifact_type for d in drafts}
        assert types == {"cover_letter", "email"}
        email_draft = next(d for d in drafts if d.artifact_type == "email")
        assert email_draft.subject is not None
        assert "AI Engineer" in email_draft.subject

    # Verify drafts API returns both documents with attachments
    resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["documents"]) == 2
    assert "John_Doe_Resume.pdf" in data["attachments"]
    assert "John_Doe_Cover_Letter.pdf" in data["attachments"]


def test_scenario_2_ai_provider_failure_produces_safe_error_code(env):
    """2. AI provider failures are mapped to informative, sanitized error codes."""
    quota_err = RuntimeError("GoogleAPIError: ResourceExhausted: 429 Quota exceeded")
    assert _safe_error_code(quota_err) == "ai_provider_quota_exceeded"

    auth_err = RuntimeError("GoogleAPIError: 401 API key not valid")
    assert _safe_error_code(auth_err) == "ai_provider_authentication_failed"

    timeout_err = TimeoutError("Gemini API call timed out after 30 seconds")
    assert _safe_error_code(timeout_err) == "ai_provider_timeout"

    model_err = RuntimeError("gemini-1.5-pro model error encountered")
    assert _safe_error_code(model_err) == "ai_provider_error"

    sessions = env["sessions"]
    owner = env["owner"]
    msg_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            status="drafting",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="running",
            phase="drafting",
            progress=55,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(OutboxMessage(
            id=msg_id,
            topic="draft_item",
            dedupe_key=f"draft:{run_id}:1",
            payload={"run_id": run_id, "batch_id": batch_id, "item_id": item_id, "owner_id": owner.user_id},
            available_at=utc_now(),
            attempts=1,
            created_at=utc_now(),
        ))

    _mark_failed(sessions, msg_id, "ai_provider_quota_exceeded")

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "failed"
        assert item.failure_code == "ai_provider_quota_exceeded"
        assert run.status == "failed"
        assert run.safe_error_code == "ai_provider_quota_exceeded"


def test_scenario_3_draft_step2_failure_preserves_saved_cover_letter(env, monkeypatch):
    """3. If email generation fails at step 2, the already-saved cover letter is preserved."""
    sessions = env["sessions"]
    client = env["client"]
    owner = env["owner"]

    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())

    artifact_file = env["artifact_dir"] / f"{artifact_id}.txt"
    artifact_file.write_text("Senior Machine Learning Engineer profile.", encoding="utf-8")

    with sessions.begin() as session:
        session.add(PrivateArtifact(
            id=artifact_id,
            owner_id=owner.user_id,
            artifact_type="cv",
            storage_key=f"{artifact_id}.txt",
            filename="CV.pdf",
            media_type="application/pdf",
            byte_size=100,
            created_at=utc_now(),
        ))
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode="open_role",
            candidate_name="Alice Smith",
            cv_artifact_id=artifact_id,
            status="drafting",
            research_decision="approved",
            research_result={
                "company_name": "OpenAI",
                "company_domain": "openai.com",
                "job_role": "Research Engineer",
                "research_summary": "Frontier AI lab",
                "contact_name": "Sam Altman",
                "contact_email": "sam@openai.com",
            },
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="running",
            phase="drafting",
            progress=55,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    # Simulate email generation failure
    from webapp import worker
    original_invoke = worker._invoke_reusable_graph
    calls = 0

    def mock_invoke(graph, config, input_data):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Cover letter succeeds
            return original_invoke(graph, config, input_data)
        # Email fails
        raise ValueError("email_generation_failed")

    monkeypatch.setattr(worker, "_invoke_reusable_graph", mock_invoke)

    with pytest.raises(ValueError, match="email_generation_failed"):
        _process_drafts(sessions, {"run_id": run_id, "item_id": item_id}, checkpointer=MemorySaver())

    # Cover letter MUST still be persisted in the database!
    with sessions() as session:
        drafts = session.scalars(
            select(DraftVersion).where(DraftVersion.item_id == item_id)
        ).all()
        assert len(drafts) == 1
        assert drafts[0].artifact_type == "cover_letter"
        assert len(drafts[0].content) > 0

    # Frontend endpoint returns the preserved cover letter even during partial failure
    resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["artifact_type"] == "cover_letter"


def test_scenario_4_resumed_workflow_uses_persisted_approved_research(env):
    """4. Resumed drafting uses persisted approved research details without researching again."""
    sessions = env["sessions"]
    owner = env["owner"]

    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())

    artifact_file = env["artifact_dir"] / f"{artifact_id}.txt"
    artifact_file.write_text("Candidate profile text.", encoding="utf-8")

    persisted_company = "Cohere Inc"
    persisted_role = "NLP Specialist"

    with sessions.begin() as session:
        session.add(PrivateArtifact(
            id=artifact_id,
            owner_id=owner.user_id,
            artifact_type="cv",
            storage_key=f"{artifact_id}.txt",
            filename="CV.pdf",
            media_type="application/pdf",
            byte_size=100,
            created_at=utc_now(),
        ))
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            candidate_name="Bob Jones",
            cv_artifact_id=artifact_id,
            status="drafting",
            research_decision="approved",
            research_result={
                "company_name": persisted_company,
                "company_domain": "cohere.com",
                "job_role": persisted_role,
                "research_summary": "LLM enterprise solutions",
                "contact_name": "Aidan Gomez",
                "contact_email": "aidan@cohere.com",
            },
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="running",
            phase="drafting",
            progress=55,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    _process_drafts(sessions, {"run_id": run_id, "item_id": item_id}, checkpointer=MemorySaver())

    with sessions() as session:
        email_draft = session.scalar(
            select(DraftVersion).where(
                DraftVersion.item_id == item_id,
                DraftVersion.artifact_type == "email",
            )
        )
        assert email_draft is not None
        assert persisted_role in email_draft.subject


def test_scenario_5_research_failure_does_not_reuse_stale_result(env, monkeypatch):
    """5. In speculative mode, research cache returns None to avoid stale or polluted cache hits."""
    state = {
        "mode": "speculative",
        "seed_query": None,
        "target_seniority": "Junior",
        "location_preference": "Remote",
        "target_domain_keywords": "AI",
        "retry_count": 0,
    }
    result = research_cache(state)
    assert result["cache_key"] is None
    assert result["research_cache_hit"] is False


def test_scenario_6_batch_deduplication_sibling_companies_excluded(env):
    """6. Multiple items in the same batch in speculative mode do not research the same company."""
    sessions = env["sessions"]
    owner = env["owner"]
    batch_id = str(uuid.uuid4())
    item1_id = str(uuid.uuid4())
    item2_id = str(uuid.uuid4())
    run2_id = str(uuid.uuid4())

    with sessions.begin() as session:
        # Item 1 already found AIETA
        session.add(BatchItem(
            id=item1_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode="speculative",
            status="research_review",
            research_result={"company_name": "AIETA", "company_domain": "aieta.org", "job_role": "AI Researcher"},
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        # Item 2 is starting research
        item2 = BatchItem(
            id=item2_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode="speculative",
            status="queued",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        run2 = ApplicationRun(
            id=run2_id,
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item2_id,
            thread_id=f"thread-{run2_id}",
            status="queued",
            progress=0,
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(item2)
        session.add(run2)

    # Sibling exclusion passed to _research_inputs
    sibling_exclusions = [{
        "status": "rejected",
        "company_name": "AIETA",
        "company_domain": "aieta.org",
        "job_role": "AI Researcher",
        "reason": "Opportunity already targeted or discovered for another candidate in this batch.",
    }]

    inputs = _research_inputs(item2, run2, "Candidate profile", sibling_exclusions=sibling_exclusions)
    rejected_names = [c["company_name"] for c in inputs["rejected_candidates"]]
    assert "AIETA" in rejected_names

    # If evaluator runs on AIETA for item 2, it is immediately rejected
    eval_state = {
        "mode": "speculative",
        "company_name": "AIETA",
        "job_role": "Junior AI Engineer",
        "rejected_candidates": inputs["rejected_candidates"],
        "retry_count": 0,
    }
    eval_result = llm_evaluates(eval_state)
    assert eval_result["research_sufficient"] is False
    assert eval_result["research_outcome"] == "no_matching_role"
    assert "already evaluated" in eval_result["evaluator_feedback"]


def test_scenario_7_repeated_search_attempts_steer_away_and_exclude(env, monkeypatch):
    """7. Search query generation and extraction actively exclude previously rejected companies."""
    # 7A: Query generator appends negative terms for rejected companies in speculative mode
    class FakeLLMResponse:
        content = "Agentic AI startups hiring junior engineers"

    class FakeLLM:
        def invoke(self, *args, **kwargs):
            return FakeLLMResponse()

    monkeypatch.setattr(legacy_config, "MOCK_MODE", False)
    monkeypatch.setattr(legacy_config, "get_llm", lambda: FakeLLM())

    state = {
        "mode": "speculative",
        "seed_query": None,
        "search_attempt": 2,
        "search_budget": 5,
        "rejected_candidates": [{"company_name": "AIETA", "job_role": "AI Engineer"}],
    }
    q_result = inputpromptformater(state)
    assert '-"AIETA"' in q_result["search_query"]

    # 7B: Formatingnode skips rejected companies when multiple are present in search results
    from evaluation.llm_evaluator import ResearchExtraction

    class FakeExtractor:
        def invoke(self, input_dict):
            messages = input_dict.get("messages", [])
            sys_msg = str(messages[0].content)
            assert "AIETA" in sys_msg  # Verify exclusion instruction reached the extraction prompt
            # Extractor picks Beta Corp instead of AIETA
            return {
                "responses": [
                    ResearchExtraction(
                        company_name="Beta Corp",
                        job_role="AI Engineer",
                        job_description="Developing LLM workflows",
                        contact_name="Sarah Connor",
                        contact_email="sarah@betacorp.io",
                        research_summary="Beta Corp is hiring AI Engineers.",
                    )
                ]
            }

    monkeypatch.setattr("agents.researcher.get_research_extractor", lambda llm: FakeExtractor())

    format_state = {
        "tavily_findings": "1. AIETA is hiring...\n2. Beta Corp is also hiring AI Engineers at https://betacorp.io",
        "rejected_candidates": [{"company_name": "AIETA", "job_role": "AI Engineer"}],
        "mode": "speculative",
    }
    res = formatingnode(format_state)
    assert res["company_name"] == "Beta Corp"
    assert res["company_name"] != "AIETA"


def test_scenario_8_frontend_draft_display_and_attachments(env):
    """8. Frontend API retrieves newly persisted drafts with version, content, and attachments."""
    sessions = env["sessions"]
    client = env["client"]
    owner = env["owner"]
    batch_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            candidate_name="Jane Doe",
            status="draft_review",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(DraftVersion(
            id=str(uuid.uuid4()),
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            artifact_type="cover_letter",
            version=1,
            content="Dear Hiring Manager,\n\nI am writing to express my interest...",
            created_at=utc_now(),
        ))
        session.add(DraftVersion(
            id=str(uuid.uuid4()),
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            artifact_type="email",
            version=1,
            subject="Application for AI Engineer — Jane Doe",
            content="Hi Dario,\n\nFollowing up regarding the AI Engineer role...",
            created_at=utc_now(),
        ))

    resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["item_id"] == item_id
    assert len(data["documents"]) == 2

    cover = next(d for d in data["documents"] if d["artifact_type"] == "cover_letter")
    assert "express my interest" in cover["content"]

    email = next(d for d in data["documents"] if d["artifact_type"] == "email")
    assert email["subject"] == "Application for AI Engineer — Jane Doe"
    assert "Following up" in email["content"]

    assert "Jane_Doe_Resume.pdf" in data["attachments"]
    assert "Jane_Doe_Cover_Letter.pdf" in data["attachments"]
