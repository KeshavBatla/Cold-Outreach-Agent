from __future__ import annotations

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
    DeliveryRecord,
    DraftVersion,
    OutboxMessage,
    OutreachBatch,
    PrivateArtifact,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.main import create_app
from webapp.settings import Settings
from webapp.worker import (
    _process_drafts,
    _process_research_decision,
)
import config as legacy_config


@pytest.fixture
def test_setup(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"
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


def _seed_drafted_item(test_setup):
    sessions = test_setup["sessions"]
    owner = test_setup["owner"]
    batch_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())

    artifact_file = test_setup["artifact_dir"] / f"{artifact_id}.txt"
    artifact_file.write_text("Experienced Python & AI Engineer with 3 years building agentic workflows.", encoding="utf-8")

    now = utc_now()
    with sessions.begin() as session:
        session.add(OutreachBatch(
            id=batch_id,
            owner_id=owner.user_id,
            status="draft_review",
            created_at=now,
            updated_at=now,
        ))
        session.add(PrivateArtifact(
            id=artifact_id,
            owner_id=owner.user_id,
            artifact_type="cv",
            storage_key=f"{artifact_id}.txt",
            filename="John_Doe_CV.pdf",
            media_type="application/pdf",
            byte_size=100,
            created_at=now,
        ))
        session.add(BatchItem(
            id=item_id,
            batch_id=batch_id,
            owner_id=owner.user_id,
            mode="open_role",
            candidate_name="John Doe",
            cv_artifact_id=artifact_id,
            status="draft_review",
            research_decision="approved",
            research_result={
                "company_name": "Anthropic",
                "company_domain": "anthropic.com",
                "job_role": "AI Engineer",
                "research_summary": "Building AI safety models",
                "contact_name": "Dario Amodei",
                "contact_email": "dario@anthropic.com",
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
            content="Dear Hiring Manager,\n\nI am writing to express my interest in the AI Engineer role.",
            created_at=now,
        ))
        session.add(DraftVersion(
            id=str(uuid.uuid4()),
            owner_id=owner.user_id,
            batch_id=batch_id,
            item_id=item_id,
            artifact_type="email",
            version=1,
            subject="AI Engineer Application — John Doe",
            content="Hi Dario,\n\nI am applying for the AI Engineer role.",
            created_at=now,
        ))
    return batch_id, item_id, run_id


def test_scenario_1_cover_letter_rejection_records_decision_and_updates_timeline(test_setup):
    """1. Rejecting a cover letter records rejection against the exact draft version without restarting research."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted_item(test_setup)

    resp = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["decision"] == "rejected"
    assert data["draft_version"] == 1

    with sessions() as session:
        batch = session.get(OutreachBatch, batch_id)
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)

        # Batch and item must NOT switch to researching!
        assert batch.status != "researching"
        assert item.status != "researching"
        assert run.status == "queued"
        assert run.phase == "drafting"

        # Check ApprovalDecision record
        decision = session.scalar(
            select(ApprovalDecision).where(
                ApprovalDecision.item_id == item_id,
                ApprovalDecision.artifact_type == "cover_letter",
            )
        )
        assert decision is not None
        assert decision.decision == "rejected"

    # Verify event timeline
    events_resp = client.get(f"/api/v1/batches/{batch_id}/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    rejection_event = next((e for e in events if e["event_type"] == "draft_rejected"), None)
    assert rejection_event is not None
    assert "Cover Letter rejected" in rejection_event["detail"]


def test_scenario_2_cover_letter_rejection_does_not_enqueue_research(test_setup):
    """2. Cover letter rejection does not enqueue any research tasks or outbox messages."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, _ = _seed_drafted_item(test_setup)

    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )

    with sessions() as session:
        outbox = session.scalars(select(OutboxMessage)).all()
        # No research messages should be created for cover letter rejection
        assert all(msg.topic != "research_item" for msg in outbox)


def test_scenario_3_cover_letter_rejection_does_not_delete_saved_email(test_setup):
    """3. Cover letter rejection does not delete or overwrite the saved email draft."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, _ = _seed_drafted_item(test_setup)

    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )

    with sessions() as session:
        email_draft = session.scalar(
            select(DraftVersion).where(
                DraftVersion.item_id == item_id,
                DraftVersion.artifact_type == "email",
            )
        )
        assert email_draft is not None
        assert "Hi Dario" in email_draft.content

    # API returns both documents
    drafts_resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    assert drafts_resp.status_code == 200
    docs = drafts_resp.json()["documents"]
    assert len(docs) == 2
    cover = next(d for d in docs if d["artifact_type"] == "cover_letter")
    email = next(d for d in docs if d["artifact_type"] == "email")
    assert cover["approval"] == "rejected"
    assert email["approval"] is None


def test_scenario_4_research_rejection_does_not_trigger_drafting(test_setup, monkeypatch):
    """4. Rejecting a research result terminates or continues research; never generates drafts."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    owner = test_setup["owner"]
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
            mode="open_role",
            candidate_name="Alice",
            status="research_review",
            research_result={
                "company_name": "Unsuitable Corp",
                "company_domain": "unsuitable.com",
                "job_role": "Junior Analyst",
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
            progress=40,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))

    # User clicks Reject on research
    resp = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/research-decision",
        json={"decision": "reject"},
    )
    assert resp.status_code == 200

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

    # Worker processes the research decision
    _process_research_decision(
        sessions,
        {"run_id": run_id, "item_id": item_id, "decision": "rejected"},
        checkpointer=None,
    )

    with sessions() as session:
        item = session.get(BatchItem, item_id)
        batch = session.get(OutreachBatch, batch_id)
        # Named company rejection is terminal
        assert item.status == "rejected"
        assert batch.status == "completed"

        # Zero drafts generated
        drafts = session.scalars(select(DraftVersion).where(DraftVersion.item_id == item_id)).all()
        assert len(drafts) == 0


def test_scenario_5_email_rejection_records_decision_without_sending(test_setup):
    """5. Rejecting an email draft records rejection and does NOT send an email or queue delivery."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, _ = _seed_drafted_item(test_setup)

    resp = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "rejected"},
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "rejected"

    with sessions() as session:
        deliveries = session.scalars(select(DeliveryRecord)).all()
        assert len(deliveries) == 0

        outbox = session.scalars(select(OutboxMessage)).all()
        assert all(m.topic != "deliver_email" for m in outbox)

        item = session.get(BatchItem, item_id)
        assert item.status != "researching"


def test_scenario_6_approving_both_drafts_leaves_batch_in_completed_state(test_setup):
    """6. Approving both drafts completes the item and batch without sending an email automatically."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted_item(test_setup)

    resp = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals/combined",
        json={"cover_letter_version": 1, "email_version": 1},
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    assert all(r["decision"] == "approved" for r in results)

    with sessions() as session:
        batch = session.get(OutreachBatch, batch_id)
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)
        assert item.status == "completed"
        assert run.status == "completed"
        assert run.progress == 100
        assert batch.status == "completed"

        # No automatic delivery created
        deliveries = session.scalars(select(DeliveryRecord)).all()
        assert len(deliveries) == 0


def test_scenario_7_regenerate_current_performs_regeneration_without_restarting_research(test_setup):
    """7. Regenerate current creates new draft versions without switching batch to researching or restarting research."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, run_id = _seed_drafted_item(test_setup)

    # User clicks "Regenerate current"
    resp = client.post(
        f"/api/v1/batches/{batch_id}/drafts/generate",
        json={"regenerate_item_ids": [item_id]},
    )
    assert resp.status_code == 202

    with sessions() as session:
        batch = session.get(OutreachBatch, batch_id)
        item = session.get(BatchItem, item_id)
        # Must be "drafting", NOT "researching"!
        assert batch.status == "drafting"
        assert item.status == "drafting"

        outbox_msg = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "draft_item")
        )
        assert outbox_msg is not None
        assert "draft:" in outbox_msg.dedupe_key

    # Worker executes draft regeneration
    checkpointer = MemorySaver()
    _process_drafts(sessions, {"run_id": run_id, "item_id": item_id}, checkpointer=checkpointer)

    with sessions() as session:
        batch = session.get(OutreachBatch, batch_id)
        item = session.get(BatchItem, item_id)
        run = session.get(ApplicationRun, run_id)

        assert item.status == "draft_review"
        assert batch.status == "draft_review"
        assert run.status == "paused"
        assert run.progress == 90

        # Version 2 drafts must exist!
        drafts = session.scalars(
            select(DraftVersion)
            .where(DraftVersion.item_id == item_id)
            .order_by(DraftVersion.version.desc())
        ).all()
        assert len(drafts) == 4  # v1 and v2 for both cover_letter and email
        latest_letter = next(d for d in drafts if d.artifact_type == "cover_letter")
        latest_email = next(d for d in drafts if d.artifact_type == "email")
        assert latest_letter.version == 2
        assert latest_email.version == 2


def test_scenario_8_duplicate_rejection_requests_are_handled_safely(test_setup):
    """8. Duplicate rejection requests are idempotent and do not create duplicate records or errors."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, _ = _seed_drafted_item(test_setup)

    resp1 = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )
    assert resp1.status_code == 200

    # Second click on Reject
    resp2 = client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )
    assert resp2.status_code == 200

    with sessions() as session:
        decisions = session.scalars(
            select(ApprovalDecision).where(
                ApprovalDecision.item_id == item_id,
                ApprovalDecision.artifact_type == "cover_letter",
            )
        ).all()
        assert len(decisions) == 1


def test_scenario_9_frontend_shows_correct_state_after_every_decision(test_setup):
    """9. Frontend API reflects the exact decision status without manual page refresh."""
    client = test_setup["client"]
    sessions = test_setup["sessions"]
    batch_id, item_id, _ = _seed_drafted_item(test_setup)

    # Initially both pending
    drafts_resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    assert drafts_resp.status_code == 200
    docs = drafts_resp.json()["documents"]
    assert all(d["approval"] is None for d in docs)

    # Reject cover letter
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "rejected"},
    )

    drafts_resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    docs = drafts_resp.json()["documents"]
    cover = next(d for d in docs if d["artifact_type"] == "cover_letter")
    email = next(d for d in docs if d["artifact_type"] == "email")
    assert cover["approval"] == "rejected"
    assert email["approval"] is None

    # Worker executes regeneration
    with sessions() as session:
        cur_run_id = session.scalar(select(ApplicationRun.id).where(ApplicationRun.item_id == item_id))
    _process_drafts(sessions, {"run_id": cur_run_id, "item_id": item_id, "rejected_artifact": "cover_letter"}, checkpointer=MemorySaver())

    drafts_resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    docs = drafts_resp.json()["documents"]
    cover = next(d for d in docs if d["artifact_type"] == "cover_letter")
    email = next(d for d in docs if d["artifact_type"] == "email")
    assert cover["version"] == 2
    assert email["version"] == 2
    assert cover["approval"] is None
    assert email["approval"] is None

    # Approve replacement email v2
    client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/approvals",
        json={"artifact_type": "email", "draft_version": 2, "decision": "approved"},
    )

    drafts_resp = client.get(f"/api/v1/batches/{batch_id}/items/{item_id}/drafts")
    docs = drafts_resp.json()["documents"]
    email = next(d for d in docs if d["artifact_type"] == "email")
    assert email["approval"] == "approved"
    assert email["delivery_status"] is None

    # Batch summary reflects draft_review
    batch_resp = client.get(f"/api/v1/batches/{batch_id}")
    assert batch_resp.status_code == 200
    assert batch_resp.json()["status"] == "draft_review"
