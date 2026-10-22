from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from persistence import Base
from webapp.settings import Settings


@lru_cache(maxsize=8)
def _engine_for(url: str) -> Engine:
    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite:") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def get_engine(settings: Settings | None = None) -> Engine:
    return _engine_for((settings or Settings.from_environment()).database_url)


def session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(settings), expire_on_commit=False)


def initialize_local_schema(settings: Settings) -> None:
    if (
        settings.app_environment not in {"development", "test"}
        or not settings.database_url.startswith("sqlite:")
    ):
        return

    repository_root = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(repository_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(repository_root / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

    engine = get_engine(settings)
    sqlite_path = engine.url.database
    if sqlite_path and sqlite_path != ":memory:" and not sqlite_path.startswith("file:"):
        Path(sqlite_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    existing_tables = set(inspect(engine).get_table_names())
    if "alembic_version" not in existing_tables:
        legacy_tables = {
            "companies",
            "contacts",
            "research_runs",
            "outreach_events",
            "exclusions",
            "research_cache",
            "reservation_guards",
            "provider_usage",
        }
        model_tables = set(Base.metadata.tables)
        if model_tables.issubset(existing_tables):
            command.stamp(alembic_config, "head")
        elif legacy_tables.issubset(existing_tables):
            command.stamp(alembic_config, "335eca5334bc")

    command.upgrade(alembic_config, "head")


def get_session() -> Generator[Session, None, None]:
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()


def database_ready(settings: Settings | None = None) -> bool:
    engine = get_engine(settings)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return inspect(engine).has_table("application_runs") and inspect(engine).has_table("alembic_version")
