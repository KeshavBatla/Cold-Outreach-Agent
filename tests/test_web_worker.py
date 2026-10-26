from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from persistence import (
    AppUser,
    ApplicationRun,
    Base,
    BatchItem,
    DraftVersion,
    OutboxMessage,
    OutreachBatch,
    RunEvent,
    utc_now,
)
from webapp.worker import _finish_research, _invoke_research_graph, _mark_running


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


def test_exhausted_speculative_search_persists_skips_without_creating_drafts(tmp_path):
    database_path = tmp_path / "exhausted-search.sqlite3"
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
            status="researching",
            created_at=now,
            updated_at=now,
        ))
        session.add(BatchItem(
            id="item",
            batch_id="batch",
            owner_id="owner",
            mode="speculative",
            status="researching",
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id="run",
            owner_id="owner",
            batch_id="batch",
            item_id="item",
            thread_id="thread",
            status="running",
            phase="research",
            progress=40,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))

    _finish_research(
        sessions,
        "run",
        "item",
        {
            "company_name": "LangChain",
            "company_domain": "langchain.com",
            "job_role": "Senior Sales Director",
            "research_summary": "No role met the candidate requirements.",
            "research_sufficient": False,
            "research_outcome": "search_exhausted",
            "search_attempt": 3,
            "search_budget": 3,
            "rejected_candidates": [{
                "status": "rejected",
                "company_name": "LangChain",
                "company_domain": "langchain.com",
                "job_role": "Senior Sales Director",
                "reason": "Role seniority and domain do not match.",
            }],
        },
        paused=False,
    )

    with sessions() as session:
        item = session.get(BatchItem, "item")
        run = session.get(ApplicationRun, "run")
        assert item.status == "ineligible"
        assert item.failure_code == "search_exhausted"
        assert item.research_result["search_attempt"] == 3
        assert item.research_result["rejected_candidates"][0]["company_name"] == "LangChain"
        assert run.status == "completed"
        assert session.query(DraftVersion).count() == 0
        events = list(session.scalars(select(RunEvent).order_by(RunEvent.sequence)))
        assert events[0].event_type == "opportunity_skipped"
        assert "LangChain" in events[0].safe_detail
        assert "3 of 3" in events[-1].safe_detail
    engine.dispose()


def test_worker_persists_speculative_search_progress_as_candidates_are_skipped(tmp_path):
    database_path = tmp_path / "search-progress.sqlite3"
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
            status="researching",
            created_at=now,
            updated_at=now,
        ))
        session.add(BatchItem(
            id="item",
            batch_id="batch",
            owner_id="owner",
            mode="speculative",
            status="researching",
            research_result={"search_budget": 4, "search_attempt": 0, "rejected_candidates": []},
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id="run",
            owner_id="owner",
            batch_id="batch",
            item_id="item",
            thread_id="thread",
            status="running",
            phase="research",
            progress=5,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))

    candidate = {
        "company_name": "LangChain",
        "company_domain": "langchain.com",
        "job_role": "Senior Sales Director",
        "reason": "Role does not meet seniority requirements.",
    }

    class StreamingGraph:
        def stream(self, _value, _config, *, stream_mode):
            assert stream_mode == "updates"
            yield {"inputpromptformater": {"search_attempt": 2, "search_budget": 4}}
            yield {
                "retry_company": {
                    "search_attempt": 2,
                    "search_budget": 4,
                    "rejected_candidates": [candidate],
                }
            }

    _invoke_research_graph(StreamingGraph(), {}, {}, sessions, "run", "item")
    with sessions() as session:
        item = session.get(BatchItem, "item")
        assert item.research_result["search_attempt"] == 2
        assert item.research_result["search_budget"] == 4
        assert item.research_result["rejected_candidates"] == [candidate]
        events = list(session.scalars(select(RunEvent).order_by(RunEvent.sequence)))
        assert [event.event_type for event in events] == [
            "search_attempt_started",
            "opportunity_skipped",
        ]
        assert "Searching opportunity 2 of 4" in events[0].safe_detail
        assert "LangChain" in events[1].safe_detail
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


def test_approved_research_creates_both_drafts_as_one_review_stage(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from webapp import worker

    database_path = tmp_path / "draft-stage.sqlite3"
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
            status="drafting",
            created_at=now,
            updated_at=now,
        ))
        session.add(BatchItem(
            id="item",
            batch_id="batch",
            owner_id="owner",
            status="drafting",
            research_decision="approved",
            research_result={
                "company_name": "Example Co",
                "job_role": "AI Engineer",
                "research_summary": "Build agentic AI systems.",
            },
            cv_artifact_id="cv",
            created_at=now,
            updated_at=now,
        ))
        session.add(ApplicationRun(
            id="run",
            owner_id="owner",
            batch_id="batch",
            item_id="item",
            thread_id="draft-stage-thread",
            status="queued",
            phase="drafting",
            progress=55,
            attempt=1,
            created_at=now,
            updated_at=now,
        ))

    class FinishedGraph:
        def __init__(self, result):
            self.result = result

        def get_state(self, _config):
            return SimpleNamespace(next=(), values={})

        def invoke(self, _initial_state, _config):
            return self.result

    monkeypatch.setattr(worker, "Settings", SimpleNamespace(from_environment=lambda: SimpleNamespace(
        private_artifact_dir=tmp_path / "private",
    )))
    monkeypatch.setattr(worker, "_private_text", lambda *_args: "CV text")
    monkeypatch.setattr(worker, "generate_candidate_profile", lambda _text: "Candidate profile")
    monkeypatch.setattr(
        worker,
        "build_coverwriter_graph",
        lambda **_kwargs: FinishedGraph({"cover_letter": "Generated cover letter"}),
    )
    monkeypatch.setattr(
        worker,
        "build_emailwriter_graph",
        lambda **_kwargs: FinishedGraph({"email_body": "Generated email"}),
    )
    monkeypatch.setattr(
        worker,
        "emaildrafter",
        lambda _state: {"subject": "Interest in the AI Engineer role"},
    )

    worker._process_drafts(
        sessions,
        {"run_id": "run", "item_id": "item"},
        checkpointer=object(),
    )

    with sessions() as session:
        item = session.get(BatchItem, "item")
        run = session.get(ApplicationRun, "run")
        drafts = list(session.scalars(
            select(worker.DraftVersion)
            .where(worker.DraftVersion.item_id == "item")
            .order_by(worker.DraftVersion.artifact_type)
        ))
        assert item.status == "draft_review"
        assert run.status == "paused"
        assert run.phase == "draft_review"
        assert {(draft.artifact_type, draft.content) for draft in drafts} == {
            ("cover_letter", "Generated cover letter"),
            ("email", "Generated email"),
        }
        assert next(draft for draft in drafts if draft.artifact_type == "email").subject == (
            "Interest in the AI Engineer role"
        )
    engine.dispose()


def test_celery_app_explicitly_imports_worker_tasks():
    from webapp.queue import celery_app

    assert "webapp.worker" in celery_app.conf.include
