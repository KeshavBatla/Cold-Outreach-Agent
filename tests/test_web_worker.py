from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from persistence import (
    AppUser,
    ApplicationRun,
    Base,
    BatchItem,
    OutboxMessage,
    OutreachBatch,
    utc_now,
)
from webapp.worker import _finish_research, _mark_running


def test_worker_updates_batch_status_with_persisted_item_progress(tmp_path):
    database_path = tmp_path / "worker.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = utc_now()
    with sessions.begin() as session:
        session.add(AppUser(
            id="owner",
            identity_provider="google",
            external_subject="subject",
            created_at=now,
            updated_at=now,
        ))
        session.add(OutreachBatch(
            id="batch",
            owner_id="owner",
            status="queued",
            created_at=now,
            updated_at=now,
        ))
        session.add(BatchItem(
            id="item",
            batch_id="batch",
            owner_id="owner",
            status="queued",
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id="run",
            owner_id="owner",
            batch_id="batch",
            item_id="item",
            thread_id="thread",
            status="queued",
            progress=0,
            attempt=0,
            created_at=now,
            updated_at=now,
        ))

    _mark_running(sessions, "run", "item", "research", 5)
    with sessions() as session:
        assert session.scalar(
            select(OutreachBatch.status).where(OutreachBatch.id == "batch")
        ) == "researching"

    _finish_research(
        sessions,
        "run",
        "item",
        {
            "company_name": "Example",
            "research_outcome": "awaiting_review",
        },
        paused=True,
    )
    with sessions() as session:
        assert session.scalar(
            select(OutreachBatch.status).where(OutreachBatch.id == "batch")
        ) == "research_review"
    engine.dispose()


def test_postgres_checkpointer_uses_encrypted_serializer_without_connecting(monkeypatch):
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

    from webapp import checkpointing

    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0123456789abcdef0123456789abcdef")
    observed = {}
    connection = object()

    @contextmanager
    def fake_connect(url, **options):
        observed["url"] = url
        observed["options"] = options
        yield connection

    class FakeSaver:
        def __init__(self, actual_connection, *, serde):
            observed["connection"] = actual_connection
            observed["serializer"] = serde

        def setup(self):
            observed["setup"] = True

    monkeypatch.setattr(checkpointing.psycopg, "connect", fake_connect)
    monkeypatch.setattr(checkpointing, "PostgresSaver", FakeSaver)
    with checkpointing.postgres_checkpointer(
        "postgresql+psycopg://user:placeholder@localhost:5432/app"
    ) as saver:
        assert isinstance(saver, FakeSaver)

    assert observed["url"] == "postgresql://user:placeholder@localhost:5432/app"
    assert observed["options"]["autocommit"] is True
    assert observed["options"]["prepare_threshold"] == 0
    assert observed["connection"] is connection
    assert isinstance(observed["serializer"], EncryptedSerializer)
    assert observed["setup"] is True


def test_research_decision_resumes_the_saved_thread_and_queues_drafts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from webapp import worker

    database_path = tmp_path / "resume.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = utc_now()
    with sessions.begin() as session:
        session.add(AppUser(
            id="owner",
            identity_provider="google",
            external_subject="subject",
            created_at=now,
            updated_at=now,
        ))
        session.add(OutreachBatch(
            id="batch",
            owner_id="owner",
            status="research_review",
            created_at=now,
            updated_at=now,
        ))
        session.add(BatchItem(
            id="item",
            batch_id="batch",
            owner_id="owner",
            status="queued",
            research_decision="approved",
            research_result={"company_name": "Example Co"},
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id="run",
            owner_id="owner",
            batch_id="batch",
            item_id="item",
            thread_id="server-owned-thread",
            status="queued",
            phase="research_review",
            progress=45,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))

    class PausedGraph:
        def __init__(self):
            self.calls = []

        def get_state(self, config):
            self.calls.append(("get_state", config))
            return SimpleNamespace(
                next=("human_evaluation",),
                values={"research_outcome": "awaiting_review"},
            )

        def update_state(self, config, values):
            self.calls.append(("update_state", config, values))

        def invoke(self, value, config):
            self.calls.append(("invoke", value, config))
            return {}

    graph = PausedGraph()
    monkeypatch.setattr(worker, "build_researcher_graph", lambda checkpointer: graph)
    worker._process_research_decision(
        sessions,
        {"run_id": "run", "item_id": "item", "decision": "approved"},
        checkpointer=object(),
    )
    with sessions() as session:
        item = session.get(BatchItem, "item")
        run = session.get(ApplicationRun, "run")
        assert item.status == "drafting"
        assert run.phase == "drafting"
        assert run.status == "queued"
        assert session.scalar(
            select(OutboxMessage.topic).where(OutboxMessage.topic == "draft_item")
        ) == "draft_item"

    config = {"configurable": {"thread_id": "server-owned-thread:research"}}
    assert graph.calls[1] == ("update_state", config, {"human_decision": "approve"})
    assert graph.calls[2] == ("invoke", None, config)
    engine.dispose()
