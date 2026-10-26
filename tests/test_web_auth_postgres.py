from __future__ import annotations

import os
import secrets
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import connect, sql
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.engine import make_url

from persistence import AppUser, LocalCredential, utc_now
from webapp.auth import hash_password
from webapp.main import create_app
from webapp.settings import Settings


pytestmark = pytest.mark.skipif(
    os.getenv("WEBAPP_AUTH_TEST_POSTGRES") != "1",
    reason="Set WEBAPP_AUTH_TEST_POSTGRES=1 to run the isolated PostgreSQL auth test.",
)


@pytest.fixture
def postgres_auth_client():
    configured_url = make_url(Settings.from_environment().database_url)
    if not configured_url.drivername.startswith("postgresql"):
        pytest.skip("Configured database is not PostgreSQL.")

    database_name = f"cold_outreach_auth_test_{secrets.token_hex(6)}"
    admin_url = configured_url.set(drivername="postgresql", database="postgres")
    raw_url = admin_url.render_as_string(hide_password=False)
    with connect(raw_url, connect_timeout=5, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                    sql.Identifier(database_name),
                    sql.Identifier(configured_url.username or "postgres"),
                )
            )

    test_url = configured_url.set(database=database_name)
    repository_root = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(repository_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(repository_root / "migrations"))
    alembic_config.set_main_option(
        "sqlalchemy.url",
        test_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    engine = None
    client = None
    try:
        command.upgrade(alembic_config, "head")
        engine = create_engine(test_url, connect_args={"connect_timeout": 5})
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions.begin() as session:
            now = utc_now()
            session.add(
                AppUser(
                    id="postgres-auth-user",
                    identity_provider="local",
                    external_subject="pg-auth@example.test",
                    email="pg-auth@example.test",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                LocalCredential(
                    user_id="postgres-auth-user",
                    login_name="pg-auth@example.test",
                    password_hash=hash_password("postgres test password"),
                    created_at=now,
                    updated_at=now,
                )
            )
        base_settings = Settings.from_environment()
        app_settings = replace(
            base_settings,
            database_url=test_url.render_as_string(hide_password=False),
            app_environment="test",
            session_cookie_secure=False,
        )
        app = create_app(settings=app_settings, sessions=sessions)
        client = TestClient(app)
        with client:
            yield client, sessions
    finally:
        if client is not None:
            client.close()
        if engine is not None:
            engine.dispose()
        with connect(raw_url, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = %s and pid <> pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
                )


def test_cookie_auth_and_protected_read_use_isolated_postgresql(postgres_auth_client):
    client, sessions = postgres_auth_client
    origin = {"Origin": "http://localhost:5173"}
    assert client.get("/api/v1/batches").status_code == 401

    invalid = client.post(
        "/api/v1/auth/login",
        headers=origin,
        json={"login": "pg-auth@example.test", "password": "wrong"},
    )
    assert invalid.status_code == 401

    login = client.post(
        "/api/v1/auth/login",
        headers=origin,
        json={"login": "pg-auth@example.test", "password": "postgres test password"},
    )
    assert login.status_code == 200
    assert login.json()["authenticated"] is True

    batches = client.get("/api/v1/batches")
    assert batches.status_code == 200, batches.text
    assert batches.json()["items"] == []
    csrf = client.cookies.get("outreach_csrf")
    assert csrf
    logout = client.post(
        "/api/v1/auth/logout",
        headers={**origin, "X-CSRF-Token": csrf},
    )
    assert logout.status_code == 204
    assert client.get("/api/v1/batches").status_code == 401
    with sessions() as session:
        assert session.query(LocalCredential).count() == 1
