from __future__ import annotations

import concurrent.futures
import threading
import uuid
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from persistence import (
    ApprovalDecision,
    AppUser,
    Base,
    BatchItem,
    DeliveryRecord,
    DraftVersion,
    GmailConnection,
    PrivateArtifact,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.gmail_delivery import decrypt_token, encrypt_token
from webapp.main import create_app
from webapp.settings import Settings


@pytest.fixture
def gmail_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "a stable local test key 32 bytes")
    database_url = f"sqlite:///{(tmp_path / 'gmail.sqlite3').as_posix()}"
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

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
        app_environment="test",
        google_oauth_client_id="test-client-id",
        google_oauth_client_secret="test-client-secret",
        google_oauth_redirect_uri="http://localhost:8000/api/v1/gmail/oauth/callback",
        gmail_test_mode=True,
        gmail_live_send_enabled=False,
    )
    app = create_app(settings=settings, sessions=sessions, readiness_check=lambda: True)
    owner = Principal(user_id="gmail-owner", subject="gmail-owner-subject", email="owner@example.test")
    app.dependency_overrides[get_current_principal] = lambda: owner

    # Create the CV file on disk so the send endpoint can locate it.
    cv_storage_path = tmp_path / "private" / "owner" / "cv.txt"
    cv_storage_path.parent.mkdir(parents=True, exist_ok=True)
    cv_storage_path.write_bytes(b"CV")

    with sessions.begin() as session:
        session.add(AppUser(
            id=owner.user_id,
            identity_provider=owner.identity_provider,
            external_subject=owner.subject,
            email=owner.email,
        ))
        session.add(PrivateArtifact(
            id="gmail-cv",
            owner_id=owner.user_id,
            batch_id=None,
            item_id=None,
            artifact_type="cv",
            storage_key="owner/cv.txt",
            filename="cv.txt",
            media_type="text/plain",
            byte_size=3,
            sha256="a" * 64,
            created_at=utc_now(),
        ))

    client = TestClient(app)
    batch_response = client.post(
        "/api/v1/batches",
        json={
            "cv_artifact_id": "gmail-cv",
            "items": [{"company_name": "Controlled Test Company", "role": "Engineer"}],
        },
    )
    assert batch_response.status_code == 201, batch_response.text
    batch = batch_response.json()
    item_id = batch["items"][0]["id"]
    draft_id = str(uuid.uuid4())
    now = utc_now()
    with sessions.begin() as session:
        item = session.get(BatchItem, item_id)
        item.status = "draft_review"
        item.research_decision = "approved"
        item.research_result = {
            "company_name": "Controlled Test Company",
            "contact_email": "controlled@example.test",
            "role_fit": "suitable",
        }
        session.add(DraftVersion(
            id=draft_id,
            owner_id=owner.user_id,
            batch_id=batch["id"],
            item_id=item_id,
            artifact_type="email",
            version=1,
            subject="A reviewed test email",
            content="This is the explicitly approved test message.",
            created_by="user",
            created_at=now,
        ))

    yield {
        "app": app,
        "client": client,
        "engine": engine,
        "sessions": sessions,
        "owner": owner,
        "batch_id": batch["id"],
        "item_id": item_id,
        "draft_id": draft_id,
    }
    client.close()
    engine.dispose()


def _path(fixture):
    return (
        f"/api/v1/gmail/batches/{fixture['batch_id']}"
        f"/items/{fixture['item_id']}/send"
    )


def _start_oauth_callback(client):
    response = client.post(
        "/api/v1/gmail/oauth/start",
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200, response.text
    state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
    return f"/api/v1/gmail/oauth/callback?state={state}&code=mock-authorization-code"


def _mock_oauth_flow(monkeypatch, credentials=None, fetch_error=None):
    import webapp.gmail_routes as gmail_routes

    class MockFlow:
        def __init__(self):
            self.credentials = credentials

        def fetch_token(self, *, code):
            assert code == "mock-authorization-code"
            if fetch_error is not None:
                raise fetch_error

    monkeypatch.setattr(
        gmail_routes,
        "create_oauth_flow",
        lambda _settings, _state: MockFlow(),
    )


def _approve_email(fixture):
    with fixture["sessions"].begin() as session:
        session.add(ApprovalDecision(
            id=str(uuid.uuid4()),
            owner_id=fixture["owner"].user_id,
            batch_id=fixture["batch_id"],
            item_id=fixture["item_id"],
            artifact_type="email",
            draft_version_id=fixture["draft_id"],
            decision="approved",
            decided_by=fixture["owner"].user_id,
            decided_at=utc_now(),
        ))


def _connect_gmail(fixture):
    with fixture["sessions"].begin() as session:
        session.add(GmailConnection(
            id="gmail-connection",
            owner_id=fixture["owner"].user_id,
            status="connected",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            token_secret_ref="encrypted-test-token",
            linked_at=utc_now(),
            updated_at=utc_now(),
        ))


def test_gmail_connection_status_and_missing_oauth_configuration(gmail_fixture):
    client = gmail_fixture["client"]
    status_response = client.get("/api/v1/gmail/connection")
    assert status_response.status_code == 200
    assert status_response.json() == {
        "connected": False,
        "status": "unlinked",
        "test_mode": True,
        "live_enabled": False,
        "account_email": None,
    }

    settings = replace(
        gmail_fixture["app"].state.settings,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_oauth_redirect_uri=None,
    )
    gmail_fixture["app"].state.settings = settings
    response = client.post("/api/v1/gmail/oauth/start")
    assert response.status_code == 503
    assert "GOOGLE_OAUTH_CLIENT_ID" in response.json()["detail"]
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in response.json()["detail"]


def test_gmail_oauth_requests_only_send_scope_and_binds_state_cookie(gmail_fixture):
    response = gmail_fixture["client"].post(
        "/api/v1/gmail/oauth/start",
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200, response.text
    parsed = urlparse(response.json()["authorization_url"])
    query = parse_qs(parsed.query)
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.send"]
    assert query["access_type"] == ["offline"]
    assert response.cookies.get("gmail_oauth_state")


def test_oauth_callback_encrypts_and_persists_refresh_credentials(gmail_fixture, monkeypatch):
    fixture = gmail_fixture

    class MockCredentials:
        scopes = ["https://www.googleapis.com/auth/gmail.send"]
        refresh_token = "never-store-this-plaintext"

        def to_json(self):
            return '{"refresh_token":"never-store-this-plaintext"}'

    class MockFlow:
        credentials = MockCredentials()

        def fetch_token(self, *, code):
            assert code == "mock-authorization-code"

    callback_url = _start_oauth_callback(fixture["client"])
    _mock_oauth_flow(monkeypatch, credentials=MockCredentials())
    callback = fixture["client"].get(callback_url, follow_redirects=False)
    assert callback.status_code == 303
    assert "gmail=connected" in callback.headers["location"]
    assert callback.headers["location"].startswith("http://localhost:5173/")
    assert "gmail_oauth_state" in callback.headers["set-cookie"]
    with fixture["sessions"]() as session:
        connection = session.scalar(select(GmailConnection))
        assert connection.status == "connected"
        assert connection.scopes == ["https://www.googleapis.com/auth/gmail.send"]
        assert "never-store-this-plaintext" not in connection.token_secret_ref
        assert decrypt_token(
            fixture["owner"].user_id,
            connection.id,
            connection.token_secret_ref,
        ) == '{"refresh_token":"never-store-this-plaintext"}'


def test_oauth_callback_logs_state_validation_failure(gmail_fixture, caplog):
    response = gmail_fixture["client"].get(
        "/api/v1/gmail/oauth/callback?state=invalid-state&code=redacted"
    )
    assert response.status_code == 400
    assert (
        "gmail_oauth_callback_failed stage=callback_state_validation "
        "category=state_validation_failed"
    ) in caplog.text
    assert "redacted" not in caplog.text


def test_oauth_callback_logs_sanitized_google_exchange_error(gmail_fixture, monkeypatch, caplog):
    from oauthlib.oauth2 import OAuth2Error

    class MockGoogleOAuthError(OAuth2Error):
        error = "invalid_grant"

        def __init__(self):
            super().__init__(
                description="authorization_code=do-not-log client_secret=also-secret",
                status_code=400,
            )

    callback_url = _start_oauth_callback(gmail_fixture["client"])
    _mock_oauth_flow(monkeypatch, fetch_error=MockGoogleOAuthError())
    response = gmail_fixture["client"].get(callback_url, follow_redirects=False)
    assert response.status_code == 303
    assert "gmail=authorization-failed" in response.headers["location"]
    assert (
        "gmail_oauth_callback_failed stage=authorization_code_exchange "
        "oauth_error_category=invalid_grant http_status=400"
    ) in caplog.text
    assert "do-not-log" not in caplog.text
    assert "also-secret" not in caplog.text


@pytest.mark.parametrize(
    ("failure_stage", "credentials"),
    [
        (
            "granted_scope_validation",
            type("MockCredentials", (), {
                "scopes": ["https://www.googleapis.com/auth/userinfo.email"],
                "refresh_token": "mock-refresh-token",
                "to_json": lambda self: "{}",
            })(),
        ),
        (
            "refresh_token_validation",
            type("MockCredentials", (), {
                "scopes": ["https://www.googleapis.com/auth/gmail.send"],
                "refresh_token": None,
                "to_json": lambda self: "{}",
            })(),
        ),
    ],
)
def test_oauth_callback_logs_validation_stage(
    gmail_fixture,
    monkeypatch,
    caplog,
    failure_stage,
    credentials,
):
    callback_url = _start_oauth_callback(gmail_fixture["client"])
    _mock_oauth_flow(monkeypatch, credentials=credentials)
    response = gmail_fixture["client"].get(callback_url, follow_redirects=False)
    assert response.status_code == 303
    assert f"stage={failure_stage}" in caplog.text
    assert "exception_class=GmailTokenError" in caplog.text


def test_oauth_callback_sanitizes_encryption_failure(gmail_fixture, monkeypatch, caplog):
    import webapp.gmail_routes as gmail_routes

    credentials = type("MockCredentials", (), {
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "refresh_token": "mock-refresh-token",
        "to_json": lambda self: "{}",
    })()
    callback_url = _start_oauth_callback(gmail_fixture["client"])
    _mock_oauth_flow(monkeypatch, credentials=credentials)

    def fail_encryption(*_args):
        raise RuntimeError("client_secret=do-not-log this encryption failed")

    monkeypatch.setattr(gmail_routes, "encrypt_token", fail_encryption)
    response = gmail_fixture["client"].get(callback_url, follow_redirects=False)
    assert response.status_code == 303
    assert "stage=credential_encryption" in caplog.text
    assert "exception_class=RuntimeError" in caplog.text
    assert "message=credential_encryption_error" in caplog.text
    assert "do-not-log" not in caplog.text


def test_oauth_callback_logs_database_persistence_failure(gmail_fixture, monkeypatch, caplog):
    from sqlalchemy.orm import Session

    credentials = type("MockCredentials", (), {
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "refresh_token": "mock-refresh-token",
        "to_json": lambda self: "{}",
    })()
    callback_url = _start_oauth_callback(gmail_fixture["client"])
    _mock_oauth_flow(monkeypatch, credentials=credentials)

    def fail_commit(_session):
        raise RuntimeError("database constraint failed authorization_code=do-not-log")

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = gmail_fixture["client"].get(callback_url, follow_redirects=False)
    assert response.status_code == 303
    assert "stage=database_persistence" in caplog.text
    assert "exception_class=RuntimeError" in caplog.text
    assert "message=database_error" in caplog.text
    assert "do-not-log" not in caplog.text


def test_encrypted_gmail_token_is_bound_to_owner_and_connection(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "a stable local test key 32 bytes")
    sealed = encrypt_token("owner-a", "connection-a", '{"refresh_token":"hidden"}')
    assert "hidden" not in sealed
    assert decrypt_token("owner-a", "connection-a", sealed) == '{"refresh_token":"hidden"}'
    with pytest.raises(Exception, match="could not be decrypted"):
        decrypt_token("owner-b", "connection-a", sealed)


def test_send_requires_connection_approval_and_valid_recipient(gmail_fixture):
    fixture = gmail_fixture
    client = fixture["client"]
    payload = {"draft_version": 1, "confirmed": True}

    missing_confirmation = client.post(
        _path(fixture),
        json={"draft_version": 1},
    )
    assert missing_confirmation.status_code == 422

    _connect_gmail(fixture)
    missing_approval = client.post(_path(fixture), json=payload)
    assert missing_approval.status_code == 409
    assert "must be approved" in missing_approval.json()["detail"]

    with fixture["sessions"].begin() as session:
        session.query(GmailConnection).delete()
    _approve_email(fixture)
    missing_connection = client.post(_path(fixture), json=payload)
    assert missing_connection.status_code == 409
    assert "Connect a Gmail account" in missing_connection.json()["detail"]

    _connect_gmail(fixture)
    with fixture["sessions"].begin() as session:
        item = session.get(BatchItem, fixture["item_id"])
        item.research_result = {
            "contact_email": "not-an-email",
            "role_fit": "suitable",
        }
    invalid_recipient = client.post(_path(fixture), json=payload)
    assert invalid_recipient.status_code == 409
    assert "valid recipient" in invalid_recipient.json()["detail"]
    with fixture["sessions"].begin() as session:
        item = session.get(BatchItem, fixture["item_id"])
        item.research_result = {
            "contact_email": "valid@example.test",
            "role_fit": "not_suitable",
        }
    ineligible = client.post(_path(fixture), json=payload)
    assert ineligible.status_code == 409
    assert "eligible research" in ineligible.json()["detail"]
    with fixture["sessions"]() as session:
        assert session.query(DeliveryRecord).count() == 0


def test_test_mode_persists_simulation_without_calling_gmail(gmail_fixture, monkeypatch):
    import webapp.gmail_routes as gmail_routes

    fixture = gmail_fixture
    _connect_gmail(fixture)
    _approve_email(fixture)

    def unexpected_provider_call(*_args, **_kwargs):
        pytest.fail("Test mode must not call Gmail.")

    monkeypatch.setattr(gmail_routes, "execute_gmail_send", unexpected_provider_call)
    result = fixture["client"].post(_path(fixture), json={"draft_version": 1, "confirmed": True})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "simulated"
    assert result.json()["draft_version"] == 1

    duplicate = fixture["client"].post(_path(fixture), json={"draft_version": 1, "confirmed": True})
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == result.json()["id"]
    with fixture["sessions"]() as session:
        records = list(session.scalars(select(DeliveryRecord)))
        assert len(records) == 1
        assert records[0].status == "simulated"
    documents = fixture["client"].get(
        f"/api/v1/batches/{fixture['batch_id']}/items/{fixture['item_id']}/drafts"
    ).json()["documents"]
    email = next(document for document in documents if document["artifact_type"] == "email")
    assert email["delivery_status"] == "simulated"


def test_live_send_records_confirmed_message_id_once(gmail_fixture, monkeypatch):
    import webapp.gmail_routes as gmail_routes

    fixture = gmail_fixture
    fixture["app"].state.settings = replace(
        fixture["app"].state.settings,
        gmail_test_mode=False,
        gmail_live_send_enabled=True,
        app_environment="development",
    )
    _connect_gmail(fixture)
    _approve_email(fixture)
    calls = []

    class FakeCredentials:
        token = "test-access-token"

        def to_json(self):
            return '{"token":"refreshed"}'

    monkeypatch.setattr(gmail_routes, "decrypt_token", lambda *_args: '{"token":"mock"}')
    monkeypatch.setattr(gmail_routes, "credentials_from_token", lambda _value: FakeCredentials())
    monkeypatch.setattr(gmail_routes, "refresh_credentials", lambda _credentials: None)
    monkeypatch.setattr(gmail_routes, "encrypt_token", lambda *_args: "encrypted-refreshed")
    monkeypatch.setattr(gmail_routes, "build_gmail_service", lambda _credentials: object())

    def send(service, recipient, subject, body, **_kwargs):
        calls.append((recipient, subject, body))
        return "gmail-message-123"

    monkeypatch.setattr(gmail_routes, "execute_gmail_send", send)
    response = fixture["client"].post(_path(fixture), json={"draft_version": 1, "confirmed": True})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    assert response.json()["provider_message_id"] == "gmail-message-123"
    assert calls == [(
        "controlled@example.test",
        "A reviewed test email",
        "This is the explicitly approved test message.",
    )]
    assert fixture["client"].post(
        _path(fixture),
        json={"draft_version": 1, "confirmed": True},
    ).json()["status"] == "sent"
    assert len(calls) == 1


def test_concurrent_send_attempts_reserve_only_one_delivery(gmail_fixture, monkeypatch):
    import webapp.gmail_routes as gmail_routes

    fixture = gmail_fixture
    fixture["app"].state.settings = replace(
        fixture["app"].state.settings,
        gmail_test_mode=False,
        gmail_live_send_enabled=True,
        app_environment="development",
    )
    _connect_gmail(fixture)
    _approve_email(fixture)

    class FakeCredentials:
        token = "test-access-token"

        def to_json(self):
            return '{"token":"refreshed"}'

    entered = threading.Event()
    release = threading.Event()
    call_count = 0
    call_lock = threading.Lock()
    monkeypatch.setattr(gmail_routes, "decrypt_token", lambda *_args: '{"token":"mock"}')
    monkeypatch.setattr(gmail_routes, "credentials_from_token", lambda _value: FakeCredentials())
    monkeypatch.setattr(gmail_routes, "refresh_credentials", lambda _credentials: None)
    monkeypatch.setattr(gmail_routes, "encrypt_token", lambda *_args: "encrypted-refreshed")
    monkeypatch.setattr(gmail_routes, "build_gmail_service", lambda _credentials: object())

    def send(*_args, **_kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
        entered.set()
        assert release.wait(timeout=10)
        return "concurrent-message"

    monkeypatch.setattr(gmail_routes, "execute_gmail_send", send)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            fixture["client"].post,
            _path(fixture),
            json={"draft_version": 1, "confirmed": True},
        )
        assert entered.wait(timeout=10)
        second = executor.submit(
            fixture["client"].post,
            _path(fixture),
            json={"draft_version": 1, "confirmed": True},
        )
        second_response = second.result(timeout=10)
        release.set()
        first_response = first.result(timeout=10)

    assert second_response.status_code == 200, second_response.text
    assert first_response.status_code == 200, first_response.text
    assert second_response.json()["status"] == "sending"
    assert first_response.json()["status"] == "sent"
    assert call_count == 1
    with fixture["sessions"]() as session:
        assert session.scalar(select(DeliveryRecord.id)) is not None
        assert session.query(DeliveryRecord).count() == 1


def test_ambiguous_provider_outcome_is_not_retried(gmail_fixture, monkeypatch):
    import webapp.gmail_routes as gmail_routes

    fixture = gmail_fixture
    fixture["app"].state.settings = replace(
        fixture["app"].state.settings,
        gmail_test_mode=False,
        gmail_live_send_enabled=True,
        app_environment="development",
    )
    _connect_gmail(fixture)
    _approve_email(fixture)
    calls = []

    class FakeCredentials:
        token = "test-access-token"

        def to_json(self):
            return '{"token":"refreshed"}'

    monkeypatch.setattr(gmail_routes, "decrypt_token", lambda *_args: '{"token":"mock"}')
    monkeypatch.setattr(gmail_routes, "credentials_from_token", lambda _value: FakeCredentials())
    monkeypatch.setattr(gmail_routes, "refresh_credentials", lambda _credentials: None)
    monkeypatch.setattr(gmail_routes, "encrypt_token", lambda *_args: "encrypted-refreshed")
    monkeypatch.setattr(gmail_routes, "build_gmail_service", lambda _credentials: object())

    def ambiguous(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("mock ambiguous response")

    monkeypatch.setattr(gmail_routes, "execute_gmail_send", ambiguous)
    response = fixture["client"].post(_path(fixture), json={"draft_version": 1, "confirmed": True})
    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
    duplicate = fixture["client"].post(_path(fixture), json={"draft_version": 1, "confirmed": True})
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "unknown"
    assert len(calls) == 1


def test_interrupted_delivery_requires_sent_folder_check_and_becomes_unknown(gmail_fixture):
    fixture = gmail_fixture
    _connect_gmail(fixture)
    _approve_email(fixture)
    delivery_id = str(uuid.uuid4())
    with fixture["sessions"].begin() as session:
        session.add(DeliveryRecord(
            id=delivery_id,
            owner_id=fixture["owner"].user_id,
            batch_id=fixture["batch_id"],
            item_id=fixture["item_id"],
            artifact_type="email",
            draft_version_id=fixture["draft_id"],
            approval_decision="approved",
            idempotency_key="interrupted-test-delivery",
            status="sending",
            queued_at=utc_now() - timedelta(minutes=6),
        ))

    path = f"/api/v1/gmail/deliveries/{delivery_id}/resolve-unknown"
    premature = fixture["client"].post(path, json={"checked_sent_folder": False})
    assert premature.status_code == 409
    resolved = fixture["client"].post(path, json={"checked_sent_folder": True})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "unknown"
    with fixture["sessions"]() as session:
        record = session.get(DeliveryRecord, delivery_id)
        assert record.safe_error_code == "manual_recovery_unknown"


def test_send_is_unauthenticated_and_owner_scoped(gmail_fixture):
    fixture = gmail_fixture
    _connect_gmail(fixture)
    _approve_email(fixture)
    unauthenticated_app = create_app(
        settings=fixture["app"].state.settings,
        sessions=fixture["sessions"],
        readiness_check=lambda: True,
    )
    with TestClient(unauthenticated_app) as client:
        unauthenticated = client.post(
            _path(fixture),
            json={"draft_version": 1, "confirmed": True},
        )
        assert unauthenticated.status_code == 401

    fixture["app"].dependency_overrides[get_current_principal] = lambda: Principal(
        user_id="other-owner",
        subject="other-owner-subject",
    )
    forbidden = fixture["client"].post(
        _path(fixture),
        json={"draft_version": 1, "confirmed": True},
    )
    assert forbidden.status_code == 404


def test_disconnect_revokes_token_and_removes_encrypted_value(gmail_fixture, monkeypatch):
    import webapp.gmail_routes as gmail_routes

    fixture = gmail_fixture
    _connect_gmail(fixture)
    revoked = []

    class FakeCredentials:
        def revoke(self, _request):
            revoked.append(True)

    monkeypatch.setattr(gmail_routes, "decrypt_token", lambda *_args: '{"token":"mock"}')
    monkeypatch.setattr(gmail_routes, "credentials_from_token", lambda _value: FakeCredentials())
    response = fixture["client"].post("/api/v1/gmail/disconnect")
    assert response.status_code == 204
    assert revoked == [True]
    with fixture["sessions"]() as session:
        connection = session.scalar(select(GmailConnection))
        assert connection.status == "revoked"
        assert connection.token_secret_ref is None
