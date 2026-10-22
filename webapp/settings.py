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
    app_environment: str = "development"

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = os.getenv("APP_DATABASE_URL") or os.getenv("DATABASE_URL") or legacy_config.DATABASE_URL
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        app_environment = os.getenv("APP_ENV", "development").casefold()
        return cls(
            database_url=database_url,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            private_artifact_dir=Path(
                os.getenv("PRIVATE_ARTIFACT_DIR", str(legacy_config.PROJECT_ROOT / "private_artifacts"))
            ).resolve(),
            max_batch_items=max(1, min(int(os.getenv("MAX_BATCH_ITEMS", "25")), 100)),
            max_draft_generations=max(1, min(int(os.getenv("MAX_DRAFT_GENERATIONS", "3")), 10)),
            test_principal_enabled=(
                os.getenv("WEBAPP_TEST_AUTH", "").lower() == "true"
                and app_environment in {"development", "test"}
                and database_url.startswith("sqlite:")
            ),
            app_environment=app_environment,
        )
