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
    Exclusion,
    OutboxMessage,
    OutreachBatch,
    PrivateArtifact,
    RunEvent,
    add_exclusion,
    get_exclusions,
    utc_now,
)
from evaluation.llm_evaluator import ResearchEvaluation
from webapp.auth import Principal, get_current_principal
from webapp.main import create_app
from webapp.schemas import BatchCreateRequest, BatchItemInput
from webapp.services import create_batch
from webapp.settings import Settings
from webapp.worker import (
    _mark_failed,
    _opportunity_skip_detail,
    _research_inputs,
    _safe_error_code,
)
from agents.researcher import inputpromptformater, llm_evaluates
import config as legacy_config


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "regressions.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
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
    owner = Principal(user_id="test-owner-id", subject="subject-1", email="owner@example.test")
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


def test_bug1_discovery_mode_explicit_capture_and_validation(app_env):
    """Bug 1: Discovery mode is captured, validated, and returned at all levels."""
    sessions = app_env["sessions"]
    owner = app_env["owner"]
    client = app_env["client"]

    # Create dummy CV artifact
    cv_id = str(uuid.uuid4())
    cv_file = app_env["artifact_dir"] / f"{cv_id}.txt"
    cv_file.write_text("Candidate CV with AI skills.", encoding="utf-8")
    with sessions.begin() as session:
        session.add(PrivateArtifact(
            id=cv_id,
            owner_id=owner.user_id,
            artifact_type="cv",
            storage_key=f"{cv_id}.txt",
            filename="cv.txt",
            media_type="text/plain",
            byte_size=30,
            created_at=utc_now(),
        ))

    # 1. open_role without company or domain must be rejected by validator
    with pytest.raises(ValueError, match="needs a company name or domain"):
        BatchCreateRequest(
            cv_artifact_id=cv_id,
            mode="open_role",
            items=[BatchItemInput(company_name="", domain="", role="Engineer")],
        )

    # 2. speculative batch succeeds without company or domain
    spec_req = BatchCreateRequest(
        cv_artifact_id=cv_id,
        mode="speculative",
        items=[BatchItemInput(role="Engineer")],
        target_seniority="Junior",
        target_domain_keywords="LLM, Agents",
    )
    with sessions() as session:
        batch = create_batch(session, owner, spec_req, max_items=10)
        session.commit()
        batch_id = batch.id

    # 3. BatchResponse and ResearchCard return mode
    resp = client.get(f"/api/v1/batches/{batch_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "speculative"
    assert data["items"][0]["mode"] == "speculative"

    cards_resp = client.get(f"/api/v1/batches/{batch_id}/research-results")
    assert cards_resp.status_code == 200
    cards = cards_resp.json()
    assert len(cards) == 1
    assert cards[0]["mode"] == "speculative"

    # 4. open_role batch never triggers speculative discovery
    open_req = BatchCreateRequest(
        cv_artifact_id=cv_id,
        mode="open_role",
        items=[BatchItemInput(company_name="Stripe", domain="stripe.com")],
    )
    with sessions() as session:
        open_batch = create_batch(session, owner, open_req, max_items=10)
        session.commit()
        open_batch_id = open_batch.id

    open_resp = client.get(f"/api/v1/batches/{open_batch_id}")
    assert open_resp.json()["mode"] == "open_role"

    state = {
        "mode": "open_role",
        "seed_query": "Stripe",
        "search_attempt": 0,
        "search_budget": 3,
    }
    prompt_res = inputpromptformater(state)
    assert prompt_res["mode"] == "open_role"


def test_bug2_rejected_companies_exclusion_and_categorization(app_env):
    """Bug 2: Exclusions are scoped, preserved with timestamps, and loaded into research."""
    owner_id = app_env["owner"].user_id
    sessions = app_env["sessions"]

    # 1. Add exclusion with explicit reason
    add_exclusion(owner_id, "CodNext", "codnext.com", "Rejected by user: User rejected candidate.")

    # Check timestamps and preservation
    exclusions = get_exclusions(owner_id)
    assert len(exclusions) >= 1
    codnext_exc = next(e for e in exclusions if "codnext" in (e["company_name"] or e["normalized_key"]).lower())
    original_timestamp = codnext_exc["created_at"]
    assert original_timestamp is not None
    assert "Rejected by user" in codnext_exc["rejection_reason"]

    # Re-adding does not overwrite timestamp
    add_exclusion(owner_id, "CodNext", "codnext.com", "Previously excluded")
    exclusions_after = get_exclusions(owner_id)
    codnext_after = next(e for e in exclusions_after if "codnext" in (e["company_name"] or e["normalized_key"]).lower())
    assert codnext_after["created_at"] == original_timestamp

    # 2. _research_inputs loads owner exclusions automatically
    batch_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    with sessions.begin() as session:
        item = BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner_id,
            mode="speculative",
            status="queued",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        run = ApplicationRun(
            id=run_id,
            owner_id=owner_id,
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="queued",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(item)
        session.add(run)

    inputs = _research_inputs(item, run, "Candidate Profile")
    rejected_names = [c["company_name"] for c in inputs["rejected_candidates"]]
    assert any("codnext" in name.lower() for name in rejected_names)

    # 3. Categorized skip detail
    skip_detail = _opportunity_skip_detail({
        "company_name": "CodNext",
        "company_domain": "codnext.com",
        "job_role": "AI Engineer",
        "reason": "Previously excluded: Company on exclusion list",
        "category": "Previously excluded",
    })
    assert "Previously excluded opportunity skipped: CodNext" in skip_detail


def test_bug3_eligibility_filtering_structured_reasons(app_env):
    """Bug 3: Structured eligibility decisions separate company relevance from role suitability."""
    # Test ResearchEvaluation model fields
    eval_model = ResearchEvaluation(
        company_relevant=True,
        role_suitable=True,
        seniority_match=False,
        experience_required="3-5 years",
        is_sufficient=False,
        reason="Rescale operates in AI simulation (relevant), but role requires 3-5 years experience which exceeds requested intern/junior seniority.",
        feedback="Search for entry-level or intern roles at Rescale or similar platforms.",
    )
    assert eval_model.company_relevant is True
    assert eval_model.role_suitable is True
    assert eval_model.seniority_match is False
    assert eval_model.is_sufficient is False
    assert "3-5 years" in eval_model.reason

    # Evaluator does not condemn entire company over a single role mismatch
    state = {
        "mode": "speculative",
        "company_name": "Rescale",
        "job_role": "Senior Engineer",
        "rejected_candidates": [
            {
                "status": "rejected",
                "company_name": "Rescale",
                "job_role": "Senior Engineer",
                "reason": "Rejected by eligibility filter: Requires 3-5 years experience",
            }
        ],
        "retry_count": 0,
    }
    # A different role at Rescale should not be immediately marked company_excluded
    different_role_state = {
        "mode": "speculative",
        "company_name": "Rescale",
        "job_role": "AI Intern",
        "rejected_candidates": state["rejected_candidates"],
        "retry_count": 0,
    }
    # In mock mode, with mock good, AI Intern at Rescale should succeed
    diff_res = llm_evaluates(different_role_state)
    assert diff_res["research_sufficient"] is True
    assert diff_res["research_outcome"] == "candidate_found"


def test_bug4_retry_sqlite_prevention_and_explicit_failure(app_env):
    """Bug 4: Worker PostgreSQL requirement prevents retrying on SQLite with clear error."""
    sessions = app_env["sessions"]
    owner = app_env["owner"]
    client = app_env["client"]

    batch_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            status="failed",
            failure_code="worker_requires_postgresql",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="failed",
            phase="research",
            safe_error_code="worker_requires_postgresql",
            attempt=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    # Retry on SQLite with worker_requires_postgresql returns 400 with explicit guidance
    resp = client.post(f"/api/v1/batches/{batch_id}/items/{item_id}/retry")
    assert resp.status_code == 400
    assert "requires PostgreSQL" in resp.json()["detail"]


def test_bug5_progress_events_standardized_terminal_outcomes(app_env):
    """Bug 5: Progress events report standardized terminal outcomes."""
    sessions = app_env["sessions"]
    owner = app_env["owner"]

    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())

    with sessions.begin() as session:
        session.add(OutreachBatch(
            id=batch_id,
            owner_id=owner.user_id,
            label="Outcomes Batch",
            status="drafting",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
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
            progress=50,
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

    # Test provider failure mapping
    _mark_failed(sessions, msg_id, "ai_provider_quota_exceeded")
    with sessions() as session:
        events = session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)).all()
        last_event = events[-1]
        assert "Provider failure: ai_provider_quota_exceeded" in last_event.safe_detail
