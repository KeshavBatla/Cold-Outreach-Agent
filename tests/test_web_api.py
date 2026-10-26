from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from persistence import (
    ApprovalDecision,
    AppUser,
    ApplicationRun,
    Base,
    BatchItem,
    DraftVersion,
    LocalCredential,
    OutboxMessage,
    OutreachBatch,
    PrivateArtifact,
    RunEvent,
    WebAuthSession,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.main import create_app
from webapp.routes import build_api_router
from webapp.settings import Settings
from webapp.auth import hash_password


@pytest.fixture
def web_fixture(tmp_path):
    database_path = tmp_path / "web.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


def _add_local_user(sessions, login="owner@example.test", user_id="local-owner"):
    with sessions.begin() as session:
        session.add(AppUser(
            id=user_id,
            identity_provider="local",
            external_subject=login,
            email=login,
        ))
        session.add(LocalCredential(
            user_id=user_id,
            login_name=login,
            password_hash=hash_password("correct horse battery staple"),
            created_at=utc_now(),
            updated_at=utc_now(),
        ))


def _auth_client(existing_app, sessions, *, raise_server_exceptions=True):
    app = create_app(
        settings=existing_app.state.settings,
        sessions=sessions,
        readiness_check=lambda: True,
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_authenticated_artifact_upload_commits_and_cleans_up_on_insert_failure(web_fixture):
    _, existing_app, sessions, _, _ = web_fixture
    _add_local_user(sessions)
    client = _auth_client(existing_app, sessions, raise_server_exceptions=False)
    origin = {"Origin": "http://localhost:5173"}
    try:
        unauthorized = client.post(
            "/api/v1/artifacts?artifact_type=cv",
            headers=origin,
            files={"file": ("unauthorized.txt", b"private cv", "text/plain")},
        )
        assert unauthorized.status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            headers=origin,
            json={"login": "owner@example.test", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get("outreach_csrf")
        assert csrf
        upload_headers = {**origin, "X-CSRF-Token": csrf}
        with sessions() as session:
            initial_artifact_count = session.query(PrivateArtifact).count()

        uploaded = client.post(
            "/api/v1/artifacts?artifact_type=cv",
            headers=upload_headers,
            files={"file": ("candidate.txt", b"private cv contents", "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        artifact_id = uploaded.json()["id"]
        private_dir = existing_app.state.settings.private_artifact_dir
        with sessions() as session:
            artifact = session.get(PrivateArtifact, artifact_id)
            assert artifact is not None
            assert artifact.owner_id == "local-owner"
            stored_path = private_dir / artifact.storage_key
        assert stored_path.read_bytes() == b"private cv contents"

        def reject_artifact_insert(*_args):
            raise RuntimeError("forced persistence failure")

        event.listen(PrivateArtifact, "before_insert", reject_artifact_insert)
        try:
            failed = client.post(
                "/api/v1/artifacts?artifact_type=cv",
                headers=upload_headers,
                files={"file": ("failed.txt", b"must be cleaned", "text/plain")},
            )
        finally:
            event.remove(PrivateArtifact, "before_insert", reject_artifact_insert)
        assert failed.status_code == 500
        assert len(list(private_dir.rglob("*.txt"))) == 1
        with sessions() as session:
            assert session.query(PrivateArtifact).count() == initial_artifact_count + 1
            assert session.get(PrivateArtifact, artifact_id) is not None
    finally:
        client.close()


def test_local_login_sets_secure_session_and_invalid_credentials_fail(web_fixture):
    _, existing_app, sessions, _, _ = web_fixture
    _add_local_user(sessions)
    client = _auth_client(existing_app, sessions)
    origin = {"Origin": "http://localhost:5173"}
    try:
        assert client.get("/api/v1/batches").status_code == 401
        invalid = client.post(
            "/api/v1/auth/login",
            headers=origin,
            json={"login": "owner@example.test", "password": "wrong password"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["detail"] == "Invalid login or password."
        assert "outreach_session" not in client.cookies

        response = client.post(
            "/api/v1/auth/login",
            headers=origin,
            json={"login": "OWNER@example.test", "password": "correct horse battery staple"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["authenticated"] is True
        assert response.json()["user"]["login"] == "owner@example.test"
        cookie_header = response.headers["set-cookie"].lower()
        assert "outreach_session=" in cookie_header
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header
        assert "outreach_csrf=" in cookie_header
        assert client.get("/api/v1/auth/session").json()["authenticated"] is True
        assert client.get("/api/v1/batches").status_code == 200
        with sessions() as session:
            auth_session = session.query(WebAuthSession).one()
            assert len(auth_session.token_hash) == 64
            assert auth_session.token_hash not in client.cookies.get("outreach_session", "")
    finally:
        client.close()


def test_cookie_auth_requires_csrf_and_logout_revokes_session(web_fixture):
    _, existing_app, sessions, _, _ = web_fixture
    _add_local_user(sessions)
    client = _auth_client(existing_app, sessions)
    origin = {"Origin": "http://localhost:5173"}
    try:
        login = client.post(
            "/api/v1/auth/login",
            headers=origin,
            json={"login": "owner@example.test", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get("outreach_csrf")
        assert csrf
        invalid_csrf = client.post(
            "/api/v1/artifacts?artifact_type=cv",
            headers=origin,
            files={"file": ("candidate.txt", b"private candidate data", "text/plain")},
        )
        assert invalid_csrf.status_code == 403
        invalid_origin = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://attacker.example", "X-CSRF-Token": csrf},
        )
        assert invalid_origin.status_code == 403
        logout = client.post(
            "/api/v1/auth/logout",
            headers={**origin, "X-CSRF-Token": csrf},
        )
        assert logout.status_code == 204
        assert client.get("/api/v1/batches").status_code == 401
        with sessions() as session:
            assert session.query(WebAuthSession).count() == 0
    finally:
        client.close()


def test_local_account_cannot_read_another_users_batches(web_fixture):
    _, existing_app, sessions, _, _ = web_fixture
    _add_local_user(sessions)
    with sessions.begin() as session:
        session.add(AppUser(
            id="local-other",
            identity_provider="local",
            external_subject="other@example.test",
            email="other@example.test",
        ))
        session.add_all([
            OutreachBatch(
                id="local-batch",
                owner_id="local-owner",
                label="Owned",
                status="queued",
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
            OutreachBatch(
                id="other-batch",
                owner_id="local-other",
                label="Not owned",
                status="queued",
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
        ])
    client = _auth_client(existing_app, sessions)
    try:
        login = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:5173"},
            json={"login": "owner@example.test", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        response = client.get("/api/v1/batches")
        assert response.status_code == 200
        assert [batch["id"] for batch in response.json()["items"]] == ["local-batch"]
    finally:
        client.close()


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
        items = list(session.scalars(select(BatchItem).where(BatchItem.batch_id == batch["id"])))
        runs = list(session.scalars(select(ApplicationRun).where(ApplicationRun.batch_id == batch["id"])))
        assert len(items) == len(runs) == 1
        assert (runs[0].item_id, runs[0].owner_id, runs[0].batch_id) == (
            items[0].id,
            items[0].owner_id,
            items[0].batch_id,
        )
        assert runs[0].status == "queued"
        assert runs[0].phase == "research"
        assert items[0].owner_id == owner.user_id

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


def test_speculative_batch_persists_mode_and_research_preferences(web_fixture):
    client, _, sessions, _, _ = web_fixture
    response = client.post(
        "/api/v1/batches",
        json={
            "mode": "speculative",
            "cv_artifact_id": "cv-a",
            "candidate_profile": "Python engineer focused on LLM applications.",
            "target_seniority": "Intern",
            "target_domain_keywords": "Agentic AI, LLMs",
            "location_preference": "Remote",
            "items": [{}],
        },
    )
    assert response.status_code == 201, response.text
    item_id = response.json()["items"][0]["id"]
    with sessions() as session:
        item = session.get(BatchItem, item_id)
        assert item.mode == "speculative"
        assert item.target_company is None
        assert item.target_domain is None
        assert item.candidate_profile == "Python engineer focused on LLM applications."
        assert item.target_seniority == "Intern"
        assert item.target_domain_keywords == "Agentic AI, LLMs"
        assert item.location_preference == "Remote"
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.item_id == item_id))
        assert run is not None
        assert (run.item_id, run.owner_id, run.batch_id) == (
            item.id,
            item.owner_id,
            item.batch_id,
        )
        assert run.status == "queued"
        assert run.phase == "research"


def test_open_role_batch_accepts_domain_without_company_name(web_fixture):
    client, _, sessions, _, _ = web_fixture
    response = client.post(
        "/api/v1/batches",
        json={
            "mode": "open_role",
            "cv_artifact_id": "cv-a",
            "items": [{"domain": "example.test"}],
        },
    )
    assert response.status_code == 201, response.text
    with sessions() as session:
        item = session.get(BatchItem, response.json()["items"][0]["id"])
        assert item.mode == "open_role"
        assert item.target_domain == "example.test"
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.item_id == item.id))
        assert run is not None
        assert (run.item_id, run.owner_id, run.batch_id) == (
            item.id,
            item.owner_id,
            item.batch_id,
        )
        assert run.status == "queued"
        assert run.phase == "research"


def test_failed_batch_run_insert_rolls_back_batch_items_and_outbox(web_fixture):
    _, existing_app, sessions, _, _ = web_fixture
    client = TestClient(existing_app, raise_server_exceptions=False)
    with sessions() as session:
        before = {
            "batches": session.query(OutreachBatch).count(),
            "items": session.query(BatchItem).count(),
            "runs": session.query(ApplicationRun).count(),
            "outbox": session.query(OutboxMessage).count(),
        }

    def reject_run_insert(*_args):
        raise RuntimeError("forced run insert failure")

    event.listen(ApplicationRun, "before_insert", reject_run_insert)
    try:
        response = client.post(
            "/api/v1/batches",
            json={
                "cv_artifact_id": "cv-a",
                "items": [{"company_name": "Example Co", "role": "Engineer"}],
            },
        )
    finally:
        event.remove(ApplicationRun, "before_insert", reject_run_insert)
        client.close()

    assert response.status_code == 500
    with sessions() as session:
        assert session.query(OutreachBatch).count() == before["batches"]
        assert session.query(BatchItem).count() == before["items"]
        assert session.query(ApplicationRun).count() == before["runs"]
        assert session.query(OutboxMessage).count() == before["outbox"]


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


def test_outbox_dispatch_processes_speculative_research_and_persists_review(web_fixture, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    client, _, sessions, _, _ = web_fixture
    response = client.post(
        "/api/v1/batches",
        json={
            "mode": "speculative",
            "cv_artifact_id": "cv-a",
            "candidate_profile": "Candidate with Python and AI experience.",
            "target_seniority": "Junior",
            "target_domain_keywords": "LLM agents",
            "location_preference": "Remote",
            "items": [{}],
        },
    )
    assert response.status_code == 201, response.text
    item_id = response.json()["items"][0]["id"]

    from webapp import worker

    settings = client.app.state.settings
    monkeypatch.setattr(worker, "_database_factory", lambda: (settings, sessions))
    monkeypatch.setattr(worker, "_private_text", lambda *_args: "CV file text")
    monkeypatch.setattr(worker, "generate_candidate_profile", lambda _text: "Generated from CV")
    monkeypatch.setattr(
        worker,
        "Settings",
        SimpleNamespace(from_environment=lambda: settings),
    )

    class PausedResearchGraph:
        def __init__(self):
            self.inputs = None
            self.checkpoint = SimpleNamespace(next=(), values={})

        def get_state(self, _config):
            return self.checkpoint

        def invoke(self, inputs, _config):
            self.inputs = inputs
            self.checkpoint = SimpleNamespace(
                next=("human_evaluation",),
                values={
                    **inputs,
                    "company_name": "Discovered AI Co",
                    "company_domain": "discovered-ai.test",
                    "contact_name": "Alex Example",
                    "contact_email": "alex@discovered-ai.test",
                    "job_role": "AI Engineer",
                    "job_description": "Build LLM agents.",
                    "research_summary": "A persisted research summary.",
                    "research_outcome": "awaiting_review",
                },
            )
            return self.checkpoint.values

    graph = PausedResearchGraph()
    monkeypatch.setattr(worker, "build_researcher_graph", lambda **_kwargs: graph)

    @contextmanager
    def fake_checkpointer(_database_url):
        yield object()

    monkeypatch.setattr(worker, "postgres_checkpointer", fake_checkpointer)

    processing_errors = []
    process_message = worker._process_message

    def capture_processing_error(factory, message_id):
        try:
            process_message(factory, message_id)
        except Exception as error:
            processing_errors.append(error)
            raise

    monkeypatch.setattr(worker, "_process_message", capture_processing_error)

    def deliver_task(name, args, task_id):
        assert name == "webapp.worker.process_outbox_message"
        assert task_id == args[0]
        worker.process_outbox_message.run(*args)

    monkeypatch.setattr(worker.celery_app, "send_task", deliver_task)
    assert worker.dispatch_outbox.run(limit=10) == 1, repr(processing_errors)

    assert graph.inputs["mode"] == "speculative"
    assert graph.inputs["seed_query"] is None
    assert graph.inputs["candidate_profile"] == "Candidate with Python and AI experience."
    assert graph.inputs["target_seniority"] == "Junior"
    assert graph.inputs["target_domain_keywords"] == "LLM agents"
    assert graph.inputs["location_preference"] == "Remote"
    from agents import researcher

    monkeypatch.setattr(researcher.app_config, "MOCK_MODE", True)
    researched_query = researcher.inputpromptformater(graph.inputs)
    assert researched_query["mode"] == "speculative"
    assert researched_query["search_query"]

    results = client.get(f"/api/v1/batches/{response.json()['id']}/research-results")
    assert results.status_code == 200
    card = results.json()[0]
    assert card["mode"] == "speculative"
    assert card["status"] == "research_review"
    assert card["company_name"] == "Discovered AI Co"
    assert card["contact_email"] == "alex@discovered-ai.test"
    assert card["job_description"] == "Build LLM agents."
    assert card["summary"] == "A persisted research summary."
    with sessions() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        )
        item = session.get(BatchItem, item_id)
        batch = session.get(OutreachBatch, response.json()["id"])
        assert message.published_at is not None
        assert message.processed_at is not None
        assert item.status == "research_review"
        assert item.research_result["contact_email"] == "alex@discovered-ai.test"
        assert batch.status == "research_review"
        assert session.scalar(
            select(RunEvent).where(RunEvent.event_type == "research_ready")
        )


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
        assert failed_item.failure_code == "workflow_failed"
        assert run.status == "failed"
        assert run.safe_error_code == "workflow_failed"
        assert session.scalar(
            select(RunEvent).where(RunEvent.event_type == "item_failed")
        ).safe_detail.find("workflow_failed") >= 0


def test_stale_processing_claim_becomes_visible_and_explicitly_retryable(web_fixture, monkeypatch):
    client, _, sessions, _, _ = web_fixture
    batch = _create_batch(client)
    item_id = batch["items"][0]["id"]
    from webapp import worker

    with sessions.begin() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        )
        message.processing_at = utc_now() - timedelta(seconds=worker._LEASE_SECONDS + 1)
        message_id = message.id
    settings = client.app.state.settings
    monkeypatch.setattr(worker, "_database_factory", lambda: (settings, sessions))
    monkeypatch.setattr(
        worker.celery_app,
        "send_task",
        lambda *_args, **_kwargs: pytest.fail("stale work must not be replayed automatically"),
    )

    assert worker.dispatch_outbox.run(limit=10) == 0
    with sessions() as session:
        message = session.get(OutboxMessage, message_id)
        item = session.get(BatchItem, item_id)
        assert message.processed_at is not None
        assert message.processing_at is None
        assert message.last_error_code == "worker_interrupted"
        assert item.status == "failed"
        assert item.failure_code == "worker_interrupted"

    retry = client.post(f"/api/v1/batches/{batch['id']}/items/{item_id}/retry")
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "queued"
    assert retry.json()["failure_code"] is None


def test_sqlite_worker_marks_queued_jobs_unsupported_instead_of_leaving_them_queued(
    web_fixture, monkeypatch
):
    from webapp import worker

    client, _, sessions, _, _ = web_fixture
    batch = _create_batch(client)
    item_id = batch["items"][0]["id"]
    settings = client.app.state.settings

    def sqlite_worker_not_supported():
        raise RuntimeError("Celery workflow workers require PostgreSQL; SQLite is test-only.")

    monkeypatch.setattr(worker, "_database_factory", sqlite_worker_not_supported)
    monkeypatch.setattr(worker, "Settings", type(
        "LocalSettings",
        (),
        {"from_environment": staticmethod(lambda: settings)},
    ))

    assert worker.dispatch_outbox.run(limit=10) == 0
    with sessions() as session:
        item = session.get(BatchItem, item_id)
        run = session.scalar(
            select(ApplicationRun).where(ApplicationRun.item_id == item_id)
        )
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == "research_item")
        )
        assert item.status == "failed"
        assert item.failure_code == "worker_requires_postgresql"
        assert run.safe_error_code == "worker_requires_postgresql"
        assert message.last_error_code == "worker_requires_postgresql"


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
