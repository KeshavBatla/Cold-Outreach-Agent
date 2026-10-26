from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import config as legacy_config


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    private_artifact_dir: Path
    max_batch_items: int
    max_draft_generations: int
    test_principal_enabled: bool
    max_research_attempts: int = 5
    speculative_search_budget: int = 10
    app_environment: str = "development"
    session_cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    gmail_test_mode: bool = True
    gmail_live_send_enabled: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = os.getenv("APP_DATABASE_URL") or os.getenv("DATABASE_URL") or legacy_config.DATABASE_URL
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        app_environment = os.getenv("APP_ENV", "development").casefold()
        default_secure_cookie = app_environment not in {"development", "test"}
        allowed_origins = tuple(
            origin.strip().rstrip("/")
            for origin in os.getenv(
                "WEB_ALLOWED_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if origin.strip()
        )
        if not allowed_origins or "*" in allowed_origins:
            raise ValueError("WEB_ALLOWED_ORIGINS must contain explicit browser origins.")
        session_cookie_secure = (
            os.getenv("SESSION_COOKIE_SECURE", str(default_secure_cookie)).lower() == "true"
        )
        if app_environment not in {"development", "test"} and not session_cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE cannot be disabled outside development/test.")
        return cls(
            database_url=database_url,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            private_artifact_dir=Path(
                os.getenv("PRIVATE_ARTIFACT_DIR", str(legacy_config.PROJECT_ROOT / "private_artifacts"))
            ).resolve(),
            max_batch_items=max(1, min(int(os.getenv("MAX_BATCH_ITEMS", "25")), 100)),
            max_draft_generations=max(1, min(int(os.getenv("MAX_DRAFT_GENERATIONS", "3")), 10)),
            max_research_attempts=max(1, min(int(os.getenv("MAX_RESEARCH_ATTEMPTS", "5")), 20)),
            speculative_search_budget=max(1, min(int(os.getenv("SPECULATIVE_SEARCH_BUDGET", "10")), 50)),
            test_principal_enabled=(
                os.getenv("WEBAPP_TEST_AUTH", "").lower() == "true"
                and app_environment in {"development", "test"}
                and database_url.startswith("sqlite:")
            ),
            app_environment=app_environment,
            session_cookie_secure=session_cookie_secure,
            allowed_origins=allowed_origins,
            google_oauth_client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
            google_oauth_client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
            google_oauth_redirect_uri=os.getenv("GOOGLE_OAUTH_REDIRECT_URI"),
            gmail_test_mode=os.getenv("GMAIL_TEST_MODE", "true").lower() == "true",
            gmail_live_send_enabled=(
                os.getenv("GMAIL_LIVE_SEND_ENABLED", "").lower() == "true"
                and app_environment != "test"
            ),
        )
