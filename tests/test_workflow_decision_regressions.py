from __future__ import annotations

import os
import uuid
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from persistence import (
    AppUser,
    ApplicationRun,
    ApprovalDecision,
    Base,
    BatchItem,
    DraftVersion,
    Exclusion,
    OutboxMessage,
    OutreachBatch,
    PrivateArtifact,
    RunEvent,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.main import create_app
from webapp.settings import Settings
from webapp.worker import (
    _finish_research,
    _process_drafts,
    _process_research_decision,
    _research_inputs,
    _mark_failed,
)
import config as legacy_config


@pytest.fixture
def regression_setup(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'test_reg.sqlite3').as_posix()}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PRIVATE_ARTIFACT_DIR", str(artifact_dir))

    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=artifact_dir,
        max_batch_items=10,
        max_draft_generations=5,
        max_research_attempts=5,
        speculative_search_budget=10,
        test_principal_enabled=True,
    )
    owner = Principal(
        user_id="user-reg",
        subject="sub-reg",
        email="reg@example.test",
        identity_provider="google",
    )
    with sessions.begin() as session:
        session.add(AppUser(
            id=owner.user_id,
            email=owner.email,
            identity_provider=owner.identity_provider,
            external_subject=owner.subject,
            created_at=utc_now(),
            updated_at=utc_now(),
        ))

    app = create_app(settings=settings, sessions=sessions, readiness_check=lambda: True)
    app.dependency_overrides[get_current_principal] = lambda: owner
    client = TestClient(app)
    return {
        "client": client,
        "sessions": sessions,
        "owner": owner,
        "settings": settings,
        "artifact_dir": artifact_dir,
    }


def _seed_drafted(env, mode="open_role"):
    sessions = env["sessions"]
    owner = env["owner"]
    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    cv_id = str(uuid.uuid4())

    now = utc_now()
    cv_file = env["artifact_dir"] / f"{cv_id}.txt"
    cv_file.write_text("Experienced Python Backend Engineer with AI background.", encoding="utf-8")

    with sessions.begin() as session:
        session.add(PrivateArtifact(
            id=cv_id,
            owner_id=owner.user_id,
            artifact_type="cv",
            storage_key=f"{cv_id}.txt",
            filename="resume.txt",
            byte_size=len(cv_file.read_bytes()),
            created_at=now,
        ))
        session.add(OutreachBatch(
            id=batch_id,
            owner_id=owner.user_id,
            status="draft_review",
            created_at=now,
            updated_at=now,
        ))
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode=mode,
            candidate_name="Alice Smith",
            status="draft_review",
            research_decision="approved",
            cv_artifact_id=cv_id,
            research_result={
                "company_name": "Acme Labs",
                "company_domain": "acmelabs.test",
                "contact_name": "Bob Director",
                "contact_email": "bob@acmelabs.test",
                "job_role": "Backend Engineer",
                "research_summary": "Acme Labs builds cloud infrastructure.",
                "search_attempt": 1,
                "search_budget": 5,
            },
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            thread_id=f"thread-{run_id}",
            status="paused",
            phase="draft_review",
            progress=90,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))
        session.add(DraftVersion(
            id=str(uuid.uuid4()),
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            artifact_type="cover_letter",
            version=1,
            content="Dear Acme Labs, I love your engineering.",
            created_by="agent",
            created_at=now,
        ))
        session.add(DraftVersion(
            id=str(uuid.uuid4()),
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            artifact_type="email",
            version=1,
            subject="Alice Smith Application",
            content="Hi Bob, please find attached my CV and letter.",
            created_by="agent",
            created_at=now,
        ))

    return batch_id, item_id, run_id


# ==============================================================================
# BUG 1 TESTS: Rejecting email queues regeneration and generates replacement email only
# ==============================================================================

def test_1_reject_email_queues_regeneration_for_exact_version(regression_setup):
    """Bug 1: Rejecting email records rejection on exact version and queues draft regeneration."""
    client = regression_setup["client"]
    sessions = regression_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted(regression_setup)

    # Approve cover letter first
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "approved"},
    )

    # Reject email draft v1
    resp = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "rejected"},
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "rejected"
    assert resp.json()["draft_version"] == 1

    with sessions() as session:
        # Check outbox has draft_item message queued
        outbox = session.scalars(select(OutboxMessage).where(OutboxMessage.topic == "draft_item")).all()
        assert len(outbox) == 1
        assert outbox[0].payload.get("rejected_artifact") == "email"

        # Cover letter approval remains approved!
        cover_draft = session.scalar(select(DraftVersion).where(
            DraftVersion.item_id == item_id, DraftVersion.artifact_type == "cover_letter"
        ))
        cover_decision = session.scalar(select(ApprovalDecision).where(
            ApprovalDecision.draft_version_id == cover_draft.id
        ))
        assert cover_decision is not None
        assert cover_decision.decision == "approved"

        # Item status is drafting
        item = session.get(BatchItem, item_id)
        assert item.status == "drafting"


def test_2_process_drafts_email_rejection_regenerates_only_email_using_approved_cover(regression_setup, monkeypatch):
    """Bug 1: Worker regenerates only email, preserves approved cover letter, returns email v2 as pending."""
    client = regression_setup["client"]
    sessions = regression_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted(regression_setup)

    # Approve cover letter
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "approved"},
    )
    # Reject email
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "rejected"},
    )

    # Mock emailwriter
    generated_emails = []
    class MockEmailGraph:
        def get_state(self, config):
            class S:
                next = None
                values = {}
            return S()
        def invoke(self, inputs, config):
            generated_emails.append(inputs)
            return {"email_body": "Regenerated Email v2 based on approved letter."}

    import webapp.worker as worker
    monkeypatch.setattr(worker, "build_emailwriter_graph", lambda checkpointer: MockEmailGraph())
    monkeypatch.setattr(worker, "emaildrafter", lambda state: {"subject": "New Subject v2"})

    # Worker executes draft regeneration with rejected_artifact="email"
    with sessions() as session:
        run = session.get(ApplicationRun, run_id)
        run_attempt = run.attempt

    _process_drafts(
        sessions,
        {"run_id": run_id, "item_id": item_id, "rejected_artifact": "email"},
        checkpointer=MemorySaver(),
    )

    # Verify results via API
    drafts_resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    assert drafts_resp.status_code == 200
    docs = drafts_resp.json()["documents"]

    cover = next(d for d in docs if d["artifact_type"] == "cover_letter")
    email = next(d for d in docs if d["artifact_type"] == "email")

    # Cover letter was NOT regenerated: still v1, still approved
    assert cover["version"] == 1
    assert cover["approval"] == "approved"

    # Email was regenerated to v2: pending review
    assert email["version"] == 2
    assert email["approval"] is None
    assert "Regenerated Email v2" in email["content"]

    # History preserved: email v1 still in database
    with sessions() as session:
        v1_email = session.scalar(select(DraftVersion).where(
            DraftVersion.item_id == item_id,
            DraftVersion.artifact_type == "email",
            DraftVersion.version == 1,
        ))
        assert v1_email is not None
        v1_decision = session.scalar(select(ApprovalDecision).where(
            ApprovalDecision.draft_version_id == v1_email.id
        ))
        assert v1_decision.decision == "rejected"


# ==============================================================================
# BUG 2 TESTS: Rejecting cover letter regenerates cover letter and dependent email
# ==============================================================================

def test_3_reject_cover_letter_invalidates_email_approval_and_queues_regeneration(regression_setup):
    """Bug 2: Rejecting cover letter invalidates prior email approval and queues regeneration."""
    client = regression_setup["client"]
    sessions = regression_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted(regression_setup)

    # Approve both drafts initially
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "approved"},
    )
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "approved"},
    )

    with sessions() as session:
        email_v1 = session.scalar(select(DraftVersion).where(
            DraftVersion.item_id == item_id, DraftVersion.artifact_type == "email"
        ))
        assert session.scalar(select(ApprovalDecision.id).where(
            ApprovalDecision.draft_version_id == email_v1.id
        )) is not None

    # Now reject cover letter v1
    resp = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )
    assert resp.status_code == 200

    with sessions() as session:
        # Prior approval on email v1 is deleted/invalidated
        email_decision = session.scalar(select(ApprovalDecision).where(
            ApprovalDecision.draft_version_id == email_v1.id
        ))
        assert email_decision is None

        # draft_item outbox message is queued with rejected_artifact=cover_letter
        outbox = session.scalar(select(OutboxMessage).where(OutboxMessage.topic == "draft_item"))
        assert outbox is not None
        assert outbox.payload.get("rejected_artifact") == "cover_letter"


def test_4_process_drafts_cover_letter_rejection_regenerates_both_documents(regression_setup, monkeypatch):
    """Bug 2: Worker regenerates cover letter first, then email from new letter; both return to pending review."""
    client = regression_setup["client"]
    sessions = regression_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted(regression_setup)

    # Reject cover letter
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )

    # Mock graphs
    class MockCoverGraph:
        def get_state(self, config):
            class S:
                next = None
                values = {}
            return S()
        def invoke(self, inputs, config):
            return {"cover_letter": "Regenerated Cover Letter v2"}

    class MockEmailGraph:
        def get_state(self, config):
            class S:
                next = None
                values = {}
            return S()
        def invoke(self, inputs, config):
            # Assert email drafter receives the newly generated cover letter!
            assert inputs.get("cover_letter") == "Regenerated Cover Letter v2"
            return {"email_body": "Regenerated Email v2 from new letter"}

    import webapp.worker as worker
    monkeypatch.setattr(worker, "build_coverwriter_graph", lambda **kwargs: MockCoverGraph())
    monkeypatch.setattr(worker, "build_emailwriter_graph", lambda **kwargs: MockEmailGraph())
    monkeypatch.setattr(worker, "emaildrafter", lambda state: {"subject": "New Letter Subject v2"})

    _process_drafts(
        sessions,
        {"run_id": run_id, "item_id": item_id, "rejected_artifact": "cover_letter"},
        checkpointer=MemorySaver(),
    )

    drafts_resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    assert drafts_resp.status_code == 200
    docs = drafts_resp.json()["documents"]

    cover = next(d for d in docs if d["artifact_type"] == "cover_letter")
    email = next(d for d in docs if d["artifact_type"] == "email")

    # Both documents are v2 and pending review
    assert cover["version"] == 2
    assert cover["approval"] is None
    assert "Regenerated Cover Letter v2" in cover["content"]

    assert email["version"] == 2
    assert email["approval"] is None
    assert "Regenerated Email v2" in email["content"]


# ==============================================================================
# BUG 3 TESTS: Speculative candidate rejection continues search without batch cancellation
# ==============================================================================

def test_5_speculative_research_rejection_enqueues_continuation_without_completing_batch(regression_setup):
    """Bug 3: Rejecting candidate in speculative discovery keeps batch researching instead of cancelling."""
    client = regression_setup["client"]
    sessions = regression_setup["sessions"]
    owner = regression_setup["owner"]
    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    now = utc_now()
    with sessions.begin() as session:
        session.add(OutreachBatch(
            id=batch_id,
            owner_id=owner.user_id,
            status="research_review",
            created_at=now,
            updated_at=now,
        ))
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode="speculative",
            candidate_name="Alice",
            status="research_review",
            research_result={
                "company_name": "Poor Fit Corp",
                "company_domain": "poorfit.test",
                "job_role": "Support Rep",
                "search_attempt": 1,
                "search_budget": 5,
            },
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
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

    # Reject candidate
    resp = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/research-decision",
        json={"decision": "reject"},
    )
    assert resp.status_code == 200

    with sessions() as session:
        batch = session.get(OutreachBatch, batch_id)
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)

        # Batch is active ("researching"), NOT "completed" or "cancelled"
        assert batch.status == "researching"
        assert item.status == "queued"
        assert run.status == "queued"
        assert run.phase == "research"


def test_6_speculative_rejection_persists_exclusion_and_enqueues_continuation(regression_setup, monkeypatch):
    """Bug 3: Rejection adds company to exclusions, records rejected candidate, and queues next search."""
    sessions = regression_setup["sessions"]
    owner = regression_setup["owner"]
    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    now = utc_now()
    with sessions.begin() as session:
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode="speculative",
            status="research_review",
            research_decision="rejected",
            research_result={
                "company_name": "Rejected Company",
                "company_domain": "rejected.test",
                "job_role": "Junior Lead",
                "search_attempt": 1,
                "search_budget": 5,
            },
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id=run_id,
            owner_id=owner.user_id,
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

    class MockGraph:
        def get_state(self, config):
            class S:
                next = None
                values = {"human_decision": "reject"}
            return S()
        def update_state(self, config, values):
            pass
        def invoke(self, input_val, config):
            pass

    import webapp.worker as worker
    monkeypatch.setattr(worker, "build_researcher_graph", lambda checkpointer: MockGraph())

    _process_research_decision(
        sessions,
        {"run_id": run_id, "item_id": item_id, "decision": "rejected"},
        checkpointer=None,
    )

    with sessions() as session:
        # Exclusion saved
        exclusion = session.scalar(select(Exclusion).where(Exclusion.owner_id == owner.user_id))
        assert exclusion is not None
        assert "rejected.test" in exclusion.normalized_key or "rejected company" in exclusion.normalized_key

        # Item records rejected candidate
        item = session.get(BatchItem, item_id)
        rejected_list = item.research_result.get("rejected_candidates")
        assert len(rejected_list) == 1
        assert rejected_list[0]["company_name"] == "Rejected Company"

        # Outbox message queued for next research attempt
        outbox = session.scalar(select(OutboxMessage).where(OutboxMessage.topic == "research_item"))
        assert outbox is not None
        assert "research-continue" in outbox.dedupe_key


# ==============================================================================
# BUG 4 TESTS: Configurable search budget and separated candidate counting
# ==============================================================================

def test_7_speculative_search_budget_is_configurable_via_env(regression_setup, monkeypatch):
    """Bug 4: SPECULATIVE_SEARCH_BUDGET is configurable via environment variable and used in inputs."""
    monkeypatch.setenv("SPECULATIVE_SEARCH_BUDGET", "12")
    settings = Settings.from_environment()
    assert settings.speculative_search_budget == 12

    item = BatchItem(
        id="item-test",
        batch_id="b-test",
        owner_id="u-test",
        mode="speculative",
        candidate_profile="Software Engineer",
        research_result={},
    )
    run = ApplicationRun(
        id="run-test",
        owner_id="u-test",
        batch_id="b-test",
        item_id="item-test",
        thread_id="t-test",
        attempt=1,
    )

    inputs = _research_inputs(item, run, "Software Engineer")
    assert inputs["search_budget"] == 12


def test_8_search_exhaustion_records_discovered_evaluated_accepted_counts(regression_setup):
    """Bug 4: Search exhaustion records discovered, evaluated, and accepted counts with rejection summary."""
    sessions = regression_setup["sessions"]
    owner = regression_setup["owner"]
    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    now = utc_now()
    with sessions.begin() as session:
        session.add(OutreachBatch(id=batch_id, owner_id=owner.user_id, status="researching", created_at=now, updated_at=now))
        session.add(BatchItem(
            id=item_id, batch_id=batch_id, owner_id=owner.user_id, mode="speculative",
            status="researching", created_at=now, updated_at=now,
        ))
        session.add(ApplicationRun(
            id=run_id, owner_id=owner.user_id, batch_id=batch_id, item_id=item_id,
            thread_id=f"thread-{run_id}", status="running", phase="research",
            progress=30, attempt=4, created_at=now, updated_at=now,
        ))

    # Worker finishes research with search_exhausted outcome
    _finish_research(
        sessions,
        run_id,
        item_id,
        {
            "company_name": None,
            "research_sufficient": False,
            "research_outcome": "search_exhausted",
            "search_attempt": 4,
            "search_budget": 4,
            "rejected_candidates": [
                {"company_name": "Company A", "reason": "No engineering roles open"},
                {"company_name": "Company B", "reason": "Seniority mismatch"},
                {"company_name": "Company C", "reason": "Domain mismatch"},
                {"company_name": "Company D", "reason": "No usable contact found"},
            ],
        },
        paused=False,
    )

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "ineligible"
        assert item.failure_code == "search_exhausted"
        assert run.status == "completed"

        res = item.research_result
        assert res["candidates_discovered"] == 4
        assert res["candidates_evaluated"] == 4
        assert res["candidates_accepted"] == 0
        assert res["search_attempt"] == 4
        assert res["search_budget"] == 4

        # Detailed event logged
        events = list(session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        last_event = events[-1]
        assert "Search completed but no suitable opportunities found after evaluating 4 candidates" in last_event.safe_detail
        assert "4 of 4" in last_event.safe_detail
        assert "Rejection summary:" in last_event.safe_detail


# ==============================================================================
# BUG 5 TESTS: Queue/Outbox/Worker State Consistency and Error Recovery
# ==============================================================================

def test_9_outbox_deduplication_prevents_duplicate_active_messages_per_attempt(regression_setup):
    """Bug 5: Repeated decision requests use deterministic deduplication keys and avoid duplicate outbox rows."""
    client = regression_setup["client"]
    sessions = regression_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted(regression_setup)

    # First rejection click
    resp1 = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "rejected"},
    )
    assert resp1.status_code == 200

    # Second rejection click on same draft version (idempotent)
    resp2 = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "rejected"},
    )
    assert resp2.status_code == 200

    with sessions() as session:
        outbox = session.scalars(select(OutboxMessage).where(OutboxMessage.topic == "draft_item")).all()
        # Exactly one outbox message exists for this attempt
        assert len(outbox) == 1


def test_10_worker_handles_failure_without_hanging_in_queued_or_running(regression_setup):
    """Bug 5: Failed outbox tasks explicitly transition items to failed status and never hang."""
    sessions = regression_setup["sessions"]
    owner = regression_setup["owner"]
    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())

    now = utc_now()
    with sessions.begin() as session:
        session.add(OutreachBatch(id=batch_id, owner_id=owner.user_id, status="drafting", created_at=now, updated_at=now))
        session.add(BatchItem(id=item_id, batch_id=batch_id, owner_id=owner.user_id, status="drafting", created_at=now, updated_at=now))
        session.add(ApplicationRun(id=run_id, owner_id=owner.user_id, batch_id=batch_id, item_id=item_id, thread_id=f"t-{run_id}", status="running", phase="drafting", progress=55, attempt=1, created_at=now, updated_at=now))
        session.add(OutboxMessage(
            id=msg_id,
            topic="draft_item",
            dedupe_key=f"draft:{run_id}:1",
            payload={"run_id": run_id, "item_id": item_id},
            available_at=now,
            attempts=1,
            created_at=now,
        ))

    # Mark failed simulates unhandled error handling
    _mark_failed(sessions, msg_id, "ai_provider_timeout")

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        msg = session.get(OutboxMessage, msg_id)

        assert item.status == "failed"
        assert item.failure_code == "ai_provider_timeout"
        assert run.status == "failed"
        assert run.safe_error_code == "ai_provider_timeout"
        assert msg.processed_at is not None
        assert msg.last_error_code == "ai_provider_timeout"

        events = list(session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        assert any(e.event_type == "item_failed" for e in events)
