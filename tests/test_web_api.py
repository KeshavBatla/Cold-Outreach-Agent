from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from persistence import (
    ApprovalDecision,
    AppUser,
    ApplicationRun,
    Base,
    BatchItem,
    DraftVersion,
    OutboxMessage,
    OutreachBatch,
    PrivateArtifact,
    RunEvent,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.main import create_app
from webapp.routes import build_api_router
from webapp.settings import Settings


@pytest.fixture
def web_fixture(tmp_path):
    database_path = tmp_path / "web.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=tmp_path / "private",
        max_batch_items=10,
        max_draft_generations=3,
        test_principal_enabled=False,
    )
    app = create_app(settings=settings, sessions=sessions, readiness_check=lambda: True)
    owner = Principal(user_id="user-a", subject="google-sub-a", email="a@example.test")
    app.dependency_overrides[get_current_principal] = lambda: owner
    client = TestClient(app)
    with sessions.begin() as session:
        session.add(AppUser(
            id=owner.user_id,
            identity_provider="google",
            external_subject=owner.subject,
            email=owner.email,
        ))
        session.add(PrivateArtifact(
            id="cv-a",
            owner_id=owner.user_id,
            batch_id=None,
            item_id=None,
            artifact_type="cv",
            storage_key="user-a/cv-a.txt",
            filename="cv.txt",
            media_type="text/plain",
            byte_size=30,
            sha256="a" * 64,
            created_at=utc_now(),
        ))
    yield client, app, sessions, owner, engine
    client.close()
    engine.dispose()


def _create_batch(client):
    response = client.post(
        "/api/v1/batches",
        json={
            "label": "Test batch",
            "cv_artifact_id": "cv-a",
            "candidate_name": "Candidate",
            "items": [{"company_name": "Example Co", "role": "Engineer"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_protected_routes_fail_closed_without_principal(web_fixture):
    client, app, *_ = web_fixture
    app.dependency_overrides.pop(get_current_principal)
    assert client.get("/api/v1/batches").status_code == 401
    assert client.get("/health/live").status_code == 200


def test_fixed_test_identity_requires_explicit_test_mode_and_sqlite(web_fixture, monkeypatch):
    from webapp.settings import Settings

    monkeypatch.setenv("WEBAPP_TEST_AUTH", "true")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_DATABASE_URL", "sqlite:///isolated-test.sqlite3")
    assert Settings.from_environment().test_principal_enabled

    monkeypatch.setenv("APP_ENV", "test")
    assert Settings.from_environment().test_principal_enabled

    monkeypatch.setenv("APP_ENV", "production")
    assert not Settings.from_environment().test_principal_enabled

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+psycopg://USER:PASSWORD@localhost:5432/app",
    )
    assert not Settings.from_environment().test_principal_enabled

    _, existing_app, sessions, _, _ = web_fixture
    unsafe_settings = Settings(
        database_url="postgresql+psycopg://USER:PASSWORD@localhost:5432/app",
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=existing_app.state.settings.private_artifact_dir,
        max_batch_items=10,
        max_draft_generations=3,
        test_principal_enabled=True,
        app_environment="test",
    )
    unsafe_app = create_app(
        settings=unsafe_settings,
        sessions=sessions,
        readiness_check=lambda: True,
    )
    assert TestClient(unsafe_app).get(
        "/api/v1/batches",
        headers={"X-Webapp-Test-Auth": "enabled"},
    ).status_code == 401

    production_settings = Settings(
        database_url=existing_app.state.settings.database_url,
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=existing_app.state.settings.private_artifact_dir,
        max_batch_items=10,
        max_draft_generations=3,
        test_principal_enabled=True,
        app_environment="production",
    )
    production_app = create_app(
        settings=production_settings,
        sessions=sessions,
        readiness_check=lambda: True,
    )
    assert TestClient(production_app).get(
        "/api/v1/batches",
        headers={"X-Webapp-Test-Auth": "enabled"},
    ).status_code == 401


def test_local_test_auth_protects_batches_and_artifacts(web_fixture, tmp_path):
    _, existing_app, sessions, _, _ = web_fixture
    settings = Settings(
        database_url=existing_app.state.settings.database_url,
        redis_url=existing_app.state.settings.redis_url,
        private_artifact_dir=tmp_path / "local-test-auth",
        max_batch_items=10,
        max_draft_generations=3,
        test_principal_enabled=True,
        app_environment="development",
    )
    app = create_app(
        settings=settings,
        sessions=sessions,
        readiness_check=lambda: True,
    )
    client = TestClient(app)
    test_auth_header = {"X-Webapp-Test-Auth": "enabled"}

    assert client.get("/api/v1/batches").status_code == 401
    assert client.get("/api/v1/batches", headers=test_auth_header).status_code == 200
    assert client.post(
        "/api/v1/artifacts?artifact_type=cv",
        files={"file": ("candidate.txt", b"private candidate data", "text/plain")},
    ).status_code == 401
    assert client.post(
        "/api/v1/artifacts?artifact_type=cv",
        headers=test_auth_header,
        files={"file": ("candidate.txt", b"private candidate data", "text/plain")},
    ).status_code == 200

    preflight = client.options(
        "/api/v1/batches",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-webapp-test-auth",
        },
    )
    assert preflight.status_code == 200
    assert "x-webapp-test-auth" in preflight.headers["access-control-allow-headers"].lower()
    client.close()


def test_development_startup_migrates_sqlite_and_serves_web_endpoints(tmp_path):
    database_path = tmp_path / "state" / "web-startup.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=tmp_path / "private",
        max_batch_items=10,
        max_draft_generations=3,
        test_principal_enabled=True,
        app_environment="development",
    )
    app = create_app(settings=settings, readiness_check=lambda: True)

    with TestClient(app) as client:
        test_auth_header = {"X-Webapp-Test-Auth": "enabled"}
        batches = client.get("/api/v1/batches?limit=50", headers=test_auth_header)
        assert batches.status_code == 200, batches.text
        assert batches.json() == {"items": [], "limit": 50, "offset": 0}

        artifact = client.post(
            "/api/v1/artifacts",
            headers=test_auth_header,
            files={"file": ("candidate.txt", b"private candidate data", "text/plain")},
        )
        assert artifact.status_code == 200, artifact.text

    engine = create_engine(database_url)
    try:
        assert inspect(engine).has_table("outreach_batches")
        assert inspect(engine).has_table("private_artifacts")
        assert inspect(engine).has_table("alembic_version")
    finally:
        engine.dispose()


def test_production_startup_does_not_initialize_sqlite_schema(tmp_path):
    database_path = tmp_path / "production.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/15",
        private_artifact_dir=tmp_path / "private",
        max_batch_items=10,
        max_draft_generations=3,
        test_principal_enabled=False,
        app_environment="production",
    )
    app = create_app(settings=settings, readiness_check=lambda: True)

    with TestClient(app):
        engine = create_engine(database_url)
        try:
            assert inspect(engine).get_table_names() == []
        finally:
            engine.dispose()


def test_batch_create_is_owner_scoped_and_uses_outbox(web_fixture):
    client, app, sessions, owner, _ = web_fixture
    batch = _create_batch(client)
    assert batch["item_count"] == 1
    assert batch["items"][0]["status"] == "queued"
    with sessions() as session:
        assert session.scalar(select(OutboxMessage).where(OutboxMessage.topic == "research_item"))
        assert session.scalar(select(ApplicationRun).where(ApplicationRun.batch_id == batch["id"]))

    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id="user-b", subject="google-sub-b"
    )
    assert client.get(f"/api/v1/batches/{batch['id']}").status_code == 404


def test_batch_create_requires_company_or_domain(web_fixture):
    client, *_ = web_fixture
    response = client.post(
        "/api/v1/batches",
        json={
            "cv_artifact_id": "cv-a",
            "items": [{"role": "Engineer"}],
        },
    )
    assert response.status_code == 422
    assert any(
        "company name or domain" in error["msg"]
        for error in response.json()["detail"]
    )


def test_all_batch_item_and_artifact_operations_are_owner_scoped(web_fixture):
    client, app, _, _, _ = web_fixture
    batch = _create_batch(client)
    item_id = batch["items"][0]["id"]
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id="user-b", subject="google-sub-b"
    )
    batch_path = f"/api/v1/batches/{batch['id']}"
    item_path = f"{batch_path}/items/{item_id}"

    assert client.get(f"{batch_path}/research-results").status_code == 404
    assert client.get(f"{batch_path}/events").status_code == 404
    assert client.get(f"{item_path}/drafts").status_code == 404
    assert client.post(
        f"{item_path}/research-decision",
        json={"decision": "approve"},
    ).status_code == 404
    assert client.put(
        f"{item_path}/drafts/1?artifact_type=email",
        json={"content": "Private", "expected_current_version": 1},
    ).status_code == 404
    assert client.post(
        f"{item_path}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "approved"},
    ).status_code == 404
    assert client.post(f"{item_path}/retry").status_code == 404
    assert client.post(
        "/api/v1/batches",
        json={
            "cv_artifact_id": "cv-a",
            "items": [{"company_name": "Other owner"}],
        },
    ).status_code == 404

    with client.stream("GET", f"{batch_path}/events/stream") as response:
        assert response.status_code == 404


def test_research_decision_is_idempotent_and_rejection_does_not_queue_drafts(web_fixture):
    client, _, sessions, owner, _ = web_fixture
    batch = _create_batch(client)
    item_id = batch["items"][0]["id"]
    with sessions.begin() as session:
        item = session.get(BatchItem, item_id)
        item.status = "research_review"
        item.research_result = {
            "company_name": "Example Co",
            "contact_email": "person@example.test",
            "job_role": "Engineer",
            "research_summary": "Public company research.",
        }

    path = f"/api/v1/batches/{batch['id']}/items/{item_id}/research-decision"
    assert client.post(path, json={"decision": "reject"}).status_code == 200
    assert client.post(path, json={"decision": "reject"}).status_code == 200
    with sessions() as session:
        assert session.scalar(select(BatchItem).where(BatchItem.id == item_id)).status == "rejected"
        topics = list(session.scalars(select(OutboxMessage.topic)))
        assert topics.count("resume_research") == 1
        assert "draft_item" not in topics
        assert session.scalar(select(RunEvent).where(RunEvent.event_type == "research_rejected"))


def test_draft_generation_cannot_race_the_approved_research_resume(web_fixture):
    client, _, sessions, _, _ = web_fixture
    batch = _create_batch(client)
    item_id = batch["items"][0]["id"]
    with sessions.begin() as session:
        item = session.get(BatchItem, item_id)
        item.status = "research_review"
        item.research_result = {"company_name": "Example Co"}

    path = f"/api/v1/batches/{batch['id']}/items/{item_id}/research-decision"
    assert client.post(path, json={"decision": "approve"}).status_code == 200
    generated = client.post(
        f"/api/v1/batches/{batch['id']}/drafts/generate",
        json={},
    )
    assert generated.status_code == 409
    with sessions() as session:
        topics = list(session.scalars(select(OutboxMessage.topic)))
        assert topics.count("resume_research") == 1
        assert "draft_item" not in topics


def test_draft_approval_is_versioned_and_edits_invalidate_current_approval(web_fixture):
    client, _, sessions, owner, _ = web_fixture
    batch = _create_batch(client)
    item_id = batch["items"][0]["id"]
    now = utc_now()
    with sessions.begin() as session:
        item = session.get(BatchItem, item_id)
        item.status = "draft_review"
        item.research_decision = "approved"
        session.add_all([
            DraftVersion(
                id=str(uuid.uuid4()), owner_id=owner.user_id, batch_id=batch["id"], item_id=item_id,
                artifact_type="cover_letter", version=1, content="Letter version one",
                created_by="agent", created_at=now,
            ),
            DraftVersion(
                id=str(uuid.uuid4()), owner_id=owner.user_id, batch_id=batch["id"], item_id=item_id,
                artifact_type="email", version=1, subject="Hello", content="Email version one",
                created_by="agent", created_at=now,
            ),
        ])
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.item_id == item_id))
        run.status = "paused"

    base = f"/api/v1/batches/{batch['id']}/items/{item_id}"
    for kind in ("cover_letter", "email"):
        response = client.post(
            f"{base}/approvals",
            json={"artifact_type": kind, "draft_version": 1, "decision": "approved"},
        )
        assert response.status_code == 200, response.text
        assert client.post(
            f"{base}/approvals",
            json={"artifact_type": kind, "draft_version": 1, "decision": "approved"},
        ).status_code == 200

    assert client.get(f"{base}/drafts").status_code == 200
    edit = client.put(
        f"{base}/drafts/1?artifact_type=cover_letter",
        json={"content": "Edited letter", "expected_current_version": 1},
    )
    assert edit.status_code == 201, edit.text
    docs = client.get(f"{base}/drafts").json()["documents"]
    letter = next(document for document in docs if document["artifact_type"] == "cover_letter")
    assert letter["version"] == 2
    assert letter["approval"] is None
    assert client.post(
        f"{base}/approvals",
        json={"artifact_type": "cover_letter", "draft_version": 1, "decision": "approved"},
    ).status_code == 409
    assert client.put(
        f"{base}/drafts/1?artifact_type=email",
        json={"content": "Stale edit", "expected_current_version": 1},
    ).status_code == 201

    regenerated = client.post(
        f"/api/v1/batches/{batch['id']}/drafts/generate",
        json={"regenerate_item_ids": [item_id]},
    )
    assert regenerated.status_code == 202, regenerated.text
    current_docs = client.get(f"{base}/drafts").json()["documents"]
    assert all(document["approval"] is None for document in current_docs)
    assert client.post(
        f"{base}/approvals",
        json={"artifact_type": "email", "draft_version": 1, "decision": "approved"},
    ).status_code == 409


def test_combined_approval_is_atomic_and_idempotent(web_fixture):
    client, _, sessions, owner, _ = web_fixture
    batch = _create_batch(client)
    item_id = batch["items"][0]["id"]
    now = utc_now()
    with sessions.begin() as session:
        item = session.get(BatchItem, item_id)
        item.status = "draft_review"
        item.research_decision = "approved"
        session.add_all([
            DraftVersion(
                id=str(uuid.uuid4()), owner_id=owner.user_id, batch_id=batch["id"],
                item_id=item_id, artifact_type="cover_letter", version=1,
                content="Letter", created_by="agent", created_at=now,
            ),
            DraftVersion(
                id=str(uuid.uuid4()), owner_id=owner.user_id, batch_id=batch["id"],
                item_id=item_id, artifact_type="email", version=1,
                subject="Hello", content="Email", created_by="agent", created_at=now,
            ),
        ])

    path = f"/api/v1/batches/{batch['id']}/items/{item_id}/approvals/combined"
    stale = client.post(
        path,
        json={"cover_letter_version": 1, "email_version": 2},
    )
    assert stale.status_code == 409
    with sessions() as session:
        assert session.scalar(select(ApprovalDecision.id)) is None

    payload = {"cover_letter_version": 1, "email_version": 1}
    approved = client.post(path, json=payload)
    assert approved.status_code == 200, approved.text
    assert {decision["artifact_type"] for decision in approved.json()} == {
        "cover_letter",
        "email",
    }
    assert client.post(path, json=payload).status_code == 200
    with sessions() as session:
        assert len(list(session.scalars(select(ApprovalDecision.id)))) == 2
        assert session.scalar(select(BatchItem.status).where(BatchItem.id == item_id)) == "completed"


def test_event_cursor_replays_only_new_owner_batch_events(web_fixture):
    client, _, sessions, owner, _ = web_fixture
    batch = _create_batch(client)
    events = client.get(f"/api/v1/batches/{batch['id']}/events").json()
    assert events
    cursor = events[0]["sequence"]
    assert client.get(
        f"/api/v1/batches/{batch['id']}/events?after={cursor}"
    ).json() == []

    other = Principal(user_id="user-b", subject="google-sub-b")
    app = client.app
    app.dependency_overrides[get_current_principal] = lambda: other
    assert client.get(f"/api/v1/batches/{batch['id']}/events").status_code == 404


def test_event_stream_replays_after_last_event_id(web_fixture):
    client, app, sessions, owner, _ = web_fixture
    batch = _create_batch(client)
    events = client.get(f"/api/v1/batches/{batch['id']}/events").json()
    assert events
    with sessions.begin() as session:
        run = session.scalar(
            select(ApplicationRun).where(ApplicationRun.batch_id == batch["id"])
        )
        session.add(RunEvent(
            id=str(uuid.uuid4()),
            owner_id=owner.user_id,
            batch_id=batch["id"],
            run_id=run.id,
            sequence=2,
            event_type="progress",
            phase="research",
            status="running",
            progress=10,
            item_id=batch["items"][0]["id"],
            safe_detail="Safe test event.",
            created_at=utc_now(),
        ))
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/v1/batches/{batch['id']}/events/stream",
        "headers": [(b"last-event-id", str(events[0]["sequence"]).encode())],
        "app": app,
    }
    request = Request(scope)
    disconnect_checks = 0

    async def is_disconnected():
        nonlocal disconnect_checks
        disconnect_checks += 1
        return disconnect_checks > 1

    request.is_disconnected = is_disconnected
    stream_route = next(
        route for route in build_api_router().routes
        if getattr(route, "path", None) == "/api/v1/batches/{batch_id}/events/stream"
    )

    async def consume():
        with sessions() as session:
            response = await stream_route.endpoint(
                batch_id=batch["id"],
                request=request,
                principal=owner,
                session=session,
            )
            return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())
    assert len(chunks) == 1
    assert "id: 2\nevent: progress" in chunks[0]


def test_private_upload_returns_reference_not_contents(web_fixture):
    client, app, _, owner, _ = web_fixture
    response = client.post(
        "/api/v1/artifacts?artifact_type=cv",
        files={"file": ("candidate.txt", b"private candidate data", "text/plain")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "private candidate data" not in response.text
    assert "storage_key" not in payload
    owner_folder = hashlib.sha256(owner.user_id.encode()).hexdigest()
    assert (
        app.state.settings.private_artifact_dir / owner_folder / f"{payload['id']}.txt"
    ).exists()


def test_private_upload_storage_key_cannot_escape_artifact_root(web_fixture):
    client, app, _, _, _ = web_fixture
    malicious_owner = Principal(
        user_id="../../outside",
        subject="trusted-subject",
    )
    app.dependency_overrides[get_current_principal] = lambda: malicious_owner
    response = client.post(
        "/api/v1/artifacts?artifact_type=cv",
        files={"file": ("candidate.txt", b"private", "text/plain")},
    )
    assert response.status_code == 200, response.text
    artifact_root = app.state.settings.private_artifact_dir.resolve()
    artifact_files = list(artifact_root.rglob(f"{response.json()['id']}.txt"))
    assert len(artifact_files) == 1
    assert artifact_files[0].is_relative_to(artifact_root)


def test_outbox_dispatch_publishes_only_committed_jobs(web_fixture, monkeypatch):
    client, _, sessions, _, _ = web_fixture
    batch = _create_batch(client)
    from webapp import worker

    settings = client.app.state.settings
    monkeypatch.setattr(worker, "_database_factory", lambda: (settings, sessions))
    dispatched = []
    monkeypatch.setattr(
        worker.celery_app,
        "send_task",
        lambda name, args, task_id: dispatched.append((name, args, task_id)),
    )

    assert worker.dispatch_outbox.run(limit=10) == 1
    assert len(dispatched) == 1
    with sessions() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        )
        assert message.published_at is not None
        assert message.processed_at is None
        assert message.payload["batch_id"] == batch["id"]
    with sessions.begin() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        )
        message.processing_at = utc_now()
        message.published_at = utc_now() - timedelta(hours=2)
    assert worker.dispatch_outbox.run(limit=10) == 0
    assert len(dispatched) == 1
    monkeypatch.setattr(
        worker,
        "postgres_checkpointer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a claimed message must not execute twice")
        ),
    )
    worker._process_message(sessions, message.id)


def test_failed_item_retry_preserves_successful_batch_items(web_fixture):
    client, _, sessions, owner, _ = web_fixture
    response = client.post(
        "/api/v1/batches",
        json={
            "label": "Partial results",
            "cv_artifact_id": "cv-a",
            "items": [
                {"company_name": "Retry Co"},
                {"company_name": "Already done Co"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    batch = response.json()
    failed_item_id, completed_item_id = [item["id"] for item in batch["items"]]
    with sessions.begin() as session:
        failed_item = session.get(BatchItem, failed_item_id)
        completed_item = session.get(BatchItem, completed_item_id)
        failed_item.status = "failed"
        failed_item.failure_code = "workflow_failed"
        completed_item.status = "completed"
        failed_run = session.scalar(
            select(ApplicationRun).where(ApplicationRun.item_id == failed_item_id)
        )
        failed_run.status = "failed"
        failed_run.phase = "research"
        failed_run.attempt = 1
        for message in session.scalars(
            select(OutboxMessage).where(OutboxMessage.payload["item_id"].as_string() == failed_item_id)
        ):
            message.processed_at = utc_now()

    retried = client.post(
        f"/api/v1/batches/{batch['id']}/items/{failed_item_id}/retry"
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    with sessions() as session:
        assert session.scalar(
            select(BatchItem.status).where(BatchItem.id == completed_item_id)
        ) == "completed"
        retry_messages = list(session.scalars(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        ))
        assert any(
            message.payload["item_id"] == failed_item_id
            and message.dedupe_key.endswith(":2")
            and message.processed_at is None
            for message in retry_messages
        )


def test_uncertain_worker_failure_is_recorded_without_automatic_replay(web_fixture, monkeypatch):
    client, _, sessions, _, _ = web_fixture
    batch = _create_batch(client)
    from webapp import worker

    with sessions() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        )
        message_id = message.id
    monkeypatch.setattr(
        worker,
        "_database_factory",
        lambda: (client.app.state.settings, sessions),
    )

    def uncertain_failure(_factory, _message_id):
        raise RuntimeError("private provider payload must not be logged")

    monkeypatch.setattr(worker, "_process_message", uncertain_failure)
    assert worker.process_outbox_message.run(message_id) is None
    with sessions() as session:
        failed_message = session.get(OutboxMessage, message_id)
        failed_item = session.get(BatchItem, batch["items"][0]["id"])
        run = session.scalar(
            select(ApplicationRun).where(ApplicationRun.item_id == failed_item.id)
        )
        assert failed_message.processed_at is not None
        assert failed_message.processing_at is None
        assert failed_message.last_error_code == "workflow_failed"
        assert failed_item.status == "failed"
        assert run.status == "failed"


def test_outbox_publish_failures_stop_at_bounded_attempt_limit(web_fixture, monkeypatch):
    client, _, sessions, _, _ = web_fixture
    batch = _create_batch(client)
    from webapp import worker

    with sessions.begin() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        )
        message.attempts = worker._MAX_PUBLISH_ATTEMPTS - 1
        message_id = message.id
    monkeypatch.setattr(
        worker,
        "_database_factory",
        lambda: (client.app.state.settings, sessions),
    )

    def broker_failure(*_args, **_kwargs):
        raise ConnectionError("broker credentials and URL are private")

    monkeypatch.setattr(worker.celery_app, "send_task", broker_failure)
    assert worker.dispatch_outbox.run(limit=10) == 0
    with sessions() as session:
        message = session.get(OutboxMessage, message_id)
        item = session.get(BatchItem, batch["items"][0]["id"])
        assert message.attempts == worker._MAX_PUBLISH_ATTEMPTS
        assert message.processed_at is not None
        assert message.last_error_code == "broker_unavailable"
        assert item.status == "failed"
