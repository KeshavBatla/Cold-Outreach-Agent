from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine, inspect

from persistence import Base


EXPECTED_EXISTING_TABLES = {
    "companies",
    "contacts",
    "research_runs",
    "outreach_events",
    "exclusions",
    "research_cache",
    "reservation_guards",
    "provider_usage",
}

EXPECTED_WEB_TABLES = {
    "app_users",
    "gmail_connections",
    "outreach_batches",
    "batch_items",
    "application_runs",
    "run_events",
    "private_artifacts",
    "draft_versions",
    "approval_decisions",
    "outbox_messages",
    "delivery_records",
}


def _migration_config(database_url: str) -> Config:
    repository_root = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(repository_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(repository_root / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return alembic_config


def test_fresh_sqlite_database_matches_existing_metadata(tmp_path):
    database_path = tmp_path / "fresh.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    alembic_config = _migration_config(database_url)

    command.upgrade(alembic_config, "335eca5334bc")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == EXPECTED_EXISTING_TABLES | {"alembic_version"}
        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == (
            EXPECTED_EXISTING_TABLES | EXPECTED_WEB_TABLES | {"alembic_version"}
        )
        assert inspector.get_foreign_keys("contacts")[0]["constrained_columns"] == ["company_id"]
        assert {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("contacts")
        } == {("owner_id", "company_id", "normalized_email")}
        assert {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("approval_decisions")
        } >= {
            ("item_id", "artifact_type", "draft_version_id"),
            ("owner_id", "item_id", "artifact_type", "draft_version_id", "decision"),
        }
        delivery_fks = inspector.get_foreign_keys("delivery_records")
        assert any(
            fk["referred_table"] == "approval_decisions"
            and fk["constrained_columns"] == [
                "owner_id", "item_id", "artifact_type", "draft_version_id", "approval_decision"
            ]
            for fk in delivery_fks
        )
        assert {
            index["name"] for index in inspector.get_indexes("outreach_events")
        } >= {
            "ix_outreach_sent_email_time",
            "ix_outreach_sent_company_time",
        }
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert differences == []
    finally:
        engine.dispose()


def test_alembic_configuration_does_not_embed_database_credentials():
    repository_root = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(repository_root / "alembic.ini"))

    assert alembic_config.get_main_option("sqlalchemy.url") == ""
    assert EXPECTED_EXISTING_TABLES | EXPECTED_WEB_TABLES == set(Base.metadata.tables)
