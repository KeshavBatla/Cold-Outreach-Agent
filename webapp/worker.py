from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

import config as legacy_config
from agents.cover_writer import build_coverwriter_graph
from agents.email_drafter import emaildrafter
from agents.email_writer import build_emailwriter_graph
from agents.researcher import build_researcher_graph
from graph.state import OutreachState
from persistence import (
    ApplicationRun,
    BatchItem,
    DraftVersion,
    Exclusion,
    OutboxMessage,
    PrivateArtifact,
    normalize_company_name,
    normalize_domain,
    utc_now,
)
from utils.cv_parser import extract_text_from_cv, generate_candidate_profile
from webapp.checkpointing import postgres_checkpointer
from webapp.database import session_factory
from webapp.queue import celery_app
from webapp.services import append_event, update_batch_status
from webapp.settings import Settings

logger = logging.getLogger("cold_outreach.worker")

_LEASE_SECONDS = 3600
_MAX_PUBLISH_ATTEMPTS = 8


def _database_factory():
    settings = Settings.from_environment()
    if not settings.database_url.startswith("postgresql"):
        raise RuntimeError("Celery workflow workers require PostgreSQL; SQLite is test-only.")
    return settings, session_factory(settings)


def _private_text(session, settings: Settings, owner_id: str, artifact_id: str | None) -> str:
    if not artifact_id:
        return ""
    artifact = session.scalar(
        select(PrivateArtifact).where(
            PrivateArtifact.id == artifact_id,
            PrivateArtifact.owner_id == owner_id,
            PrivateArtifact.artifact_type.in_(("cv", "example_letter")),
        )
    )
    if artifact is None:
        raise ValueError("private_artifact_missing")
    path = (settings.private_artifact_dir / artifact.storage_key).resolve()
    if not path.is_relative_to(settings.private_artifact_dir):
        raise ValueError("private_artifact_invalid")
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8")
    return extract_text_from_cv(path)


def _state_snapshot(factory, run_id: str, item_id: str):
    with factory() as session:
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run_id))
        item = session.scalar(select(BatchItem).where(BatchItem.id == item_id))
        if run is None or item is None or run.item_id != item.id or run.owner_id != item.owner_id:
            raise ValueError("run_or_item_missing")
        return run, item


def _mark_running(factory, run_id: str, item_id: str, phase: str, progress: int) -> None:
    with factory.begin() as session:
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run_id))
        item = session.scalar(select(BatchItem).where(BatchItem.id == item_id))
        if run is None or item is None or run.owner_id != item.owner_id:
            raise ValueError("run_or_item_missing")
        run.status = "running"
        run.phase = phase
        run.progress = progress
        run.attempt = max(run.attempt, 1)
        run.started_at = run.started_at or utc_now()
        run.updated_at = utc_now()
        item.status = "researching" if phase == "research" else "drafting"
        item.updated_at = utc_now()
        update_batch_status(session, run.batch_id, run.owner_id)
        append_event(
            session, owner_id=run.owner_id, batch_id=run.batch_id, run_id=run.id,
            event_type="phase_started", phase=phase, status="running",
            progress=progress, item_id=item.id,
            detail=f"{phase.title()} started.",
        )


def _finish_research(factory, run_id: str, item_id: str, values: dict[str, Any], paused: bool) -> None:
    with factory.begin() as session:
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run_id))
        item = session.scalar(select(BatchItem).where(BatchItem.id == item_id))
        if run is None or item is None:
            raise ValueError("run_or_item_missing")
        result = {
            "company_name": values.get("company_name"),
            "company_domain": values.get("company_domain"),
            "contact_name": values.get("contact_name"),
            "contact_email": values.get("contact_email"),
            "job_role": values.get("job_role"),
            "job_description": values.get("job_description"),
            "research_summary": values.get("research_summary"),
            "role_fit": "suitable" if values.get("research_outcome") == "awaiting_review" else "not_suitable",
        }
        item.research_result = result
        item.target_company = result["company_name"] or item.target_company
        item.target_domain = result["company_domain"] or item.target_domain
        item.target_role = result["job_role"] or item.target_role
        item.status = "research_review" if paused else (
            "ineligible" if values.get("research_outcome") in {"no_matching_role", "no_usable_contact"} else "failed"
        )
        run.status = "paused" if paused else ("completed" if item.status == "ineligible" else "failed")
        run.phase = "research_review"
        run.progress = 45 if paused else 100
        run.safe_error_code = None if paused or item.status == "ineligible" else "research_failed"
        run.updated_at = utc_now()
        run.finished_at = None if paused else utc_now()
        update_batch_status(session, run.batch_id, run.owner_id)
        append_event(
            session,
            owner_id=run.owner_id,
            batch_id=run.batch_id,
            run_id=run.id,
            event_type="research_ready" if paused else "research_finished",
            phase="research_review",
            status=run.status,
            progress=run.progress,
            item_id=item.id,
            detail="Research result is ready for review." if paused else "Research did not produce a reviewable result.",
        )


def _research_inputs(item: BatchItem, run: ApplicationRun, candidate_profile: str) -> dict[str, Any]:
    return {
        "messages": [],
        "seed_query": item.target_company or item.target_domain,
        "candidate_profile": candidate_profile,
        "mode": "open_role",
        "target_domain_keywords": item.target_role,
        "location_preference": None,
        "retry_count": 0,
        "max_retries": legacy_config.MAX_RETRIES,
        "research_run_id": run.id,
        "owner_id": run.owner_id,
        "contact_attempts": 0,
        "research_outcome": None,
        "research_error": None,
        "research_cache_hit": False,
    }


def _process_research(factory, payload: dict[str, Any], checkpointer) -> None:
    run, item = _state_snapshot(factory, payload["run_id"], payload["item_id"])
    if item.status in {"research_review", "rejected", "ineligible", "draft_review", "completed"}:
        return
    graph = build_researcher_graph(checkpointer=checkpointer)
    thread_config = {"configurable": {"thread_id": f"{run.thread_id}:research"}}
    checkpoint = graph.get_state(thread_config)
    _mark_running(factory, run.id, item.id, "research", 5)
    if checkpoint.next and checkpoint.values.get("research_outcome") == "awaiting_review":
        values = checkpoint.values
    elif checkpoint.next:
        graph.invoke(None, thread_config)
        checkpoint = graph.get_state(thread_config)
        values = checkpoint.values
    elif checkpoint.values:
        values = checkpoint.values
    else:
        settings = Settings.from_environment()
        with factory() as session:
            raw_cv = _private_text(session, settings, run.owner_id, item.cv_artifact_id)
            profile = generate_candidate_profile(raw_cv) if raw_cv else ""
        graph.invoke(_research_inputs(item, run, profile), thread_config)
        checkpoint = graph.get_state(thread_config)
        values = checkpoint.values
    paused = bool(checkpoint.next) and values.get("research_outcome") == "awaiting_review"
    _finish_research(factory, run.id, item.id, values, paused)


def _queue_next_stage(session, run: ApplicationRun, item: BatchItem) -> None:
    dedupe_key = f"draft:{run.id}:{run.attempt}"
    if session.scalar(select(OutboxMessage.id).where(OutboxMessage.dedupe_key == dedupe_key)):
        return
    session.add(OutboxMessage(
        id=str(uuid.uuid4()),
        topic="draft_item",
        dedupe_key=dedupe_key,
        payload={
            "run_id": run.id,
            "batch_id": run.batch_id,
            "item_id": item.id,
            "owner_id": run.owner_id,
            "thread_id": run.thread_id,
        },
        available_at=utc_now(),
        attempts=0,
        created_at=utc_now(),
    ))


def _process_research_decision(factory, payload: dict[str, Any], checkpointer) -> None:
    run, item = _state_snapshot(factory, payload["run_id"], payload["item_id"])
    if run.status in {"completed", "failed"}:
        return
    graph = build_researcher_graph(checkpointer=checkpointer)
    thread_config = {"configurable": {"thread_id": f"{run.thread_id}:research"}}
    checkpoint = graph.get_state(thread_config)
    decision = payload.get("decision")
    if decision not in {"approved", "rejected"} or item.research_decision != decision:
        raise ValueError("research_decision_mismatch")
    expected_graph_decision = "approve" if decision == "approved" else "reject"
    if checkpoint.next:
        graph.update_state(thread_config, {"human_decision": expected_graph_decision})
        graph.invoke(None, thread_config)
    elif checkpoint.values.get("human_decision") == expected_graph_decision:
        pass
    else:
        raise ValueError("research_checkpoint_unavailable")
    with factory.begin() as session:
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run.id))
        item = session.scalar(select(BatchItem).where(BatchItem.id == item.id))
        if run is None or item is None:
            raise ValueError("run_or_item_missing")
        if decision == "rejected":
            company = (item.research_result or {}).get("company_name")
            domain = (item.research_result or {}).get("company_domain")
            if company:
                key = normalize_domain(domain) or normalize_company_name(company)
                existing = session.scalar(select(Exclusion.id).where(
                    Exclusion.owner_id == run.owner_id,
                    Exclusion.normalized_key.in_(
                        [f"domain:{key}", f"name:{normalize_company_name(company)}"]
                        if normalize_domain(domain)
                        else [f"name:{key}"]
                    ),
                ))
                if not existing:
                    session.add(Exclusion(
                        owner_id=run.owner_id,
                        normalized_key=f"domain:{key}" if normalize_domain(domain) else f"name:{key}",
                        rejection_reason="Research candidate rejected by user.",
                        created_at=utc_now(),
                    ))
            item.status = "rejected"
            run.status = "completed"
            run.progress = 100
            run.finished_at = utc_now()
            event_type = "research_rejection_applied"
        else:
            item.status = "drafting"
            run.status = "queued"
            run.phase = "drafting"
            run.progress = 50
            _queue_next_stage(session, run, item)
            event_type = "research_approval_applied"
        run.updated_at = utc_now()
        update_batch_status(session, run.batch_id, run.owner_id)
        append_event(
            session, owner_id=run.owner_id, batch_id=run.batch_id, run_id=run.id,
            event_type=event_type, phase=run.phase, status=run.status,
            progress=run.progress, item_id=item.id,
            detail="Research decision applied to the saved run.",
        )


def _process_drafts(factory, payload: dict[str, Any], checkpointer) -> None:
    run, item = _state_snapshot(factory, payload["run_id"], payload["item_id"])
    if item.research_decision != "approved":
        raise ValueError("research_not_approved")
    if item.status in {"draft_review", "completed"}:
        return
    if not item.cv_artifact_id:
        raise ValueError("cv_artifact_missing")

    settings = Settings.from_environment()
    with factory() as session:
        raw_cv = _private_text(session, settings, run.owner_id, item.cv_artifact_id)
        profile = generate_candidate_profile(raw_cv)
        result = item.research_result or {}

    def load_artifact(artifact_id: str) -> str:
        with factory() as artifact_session:
            return _private_text(artifact_session, settings, run.owner_id, artifact_id)

    _mark_running(factory, run.id, item.id, "drafting", 55)
    state: OutreachState = {
        "messages": [],
        "company_name": result.get("company_name"),
        "company_domain": result.get("company_domain"),
        "contact_name": result.get("contact_name"),
        "contact_email": result.get("contact_email"),
        "job_role": result.get("job_role"),
        "job_description": result.get("job_description"),
        "research_summary": result.get("research_summary"),
        "candidate_name": item.candidate_name,
        "candidate_profile": profile,
        "research_run_id": run.id,
        "run_dir": None,
        "retry_count": 0,
        "max_retries": legacy_config.MAX_RETRIES,
        "cover_letter_attempts": 0,
        "email_attempts": 0,
        "cover_letter_approved": False,
        "email_approved": False,
        "cv_artifact_id": item.cv_artifact_id,
        "example_letter_artifact_id": item.example_letter_artifact_id,
    }
    cover_graph = build_coverwriter_graph(
        checkpointer=checkpointer,
        private_artifact_loader=load_artifact,
    )
    cover_config = {"configurable": {"thread_id": f"{run.thread_id}_cover_{run.attempt}"}}
    cover_input = {
        "messages": [],
        "company_name": result.get("company_name"),
        "job_role": result.get("job_role"),
        "job_description": result.get("job_description"),
        "research_summary": result.get("research_summary"),
        "research_run_id": run.id,
        "raw_cv_text": None,
        "cv_artifact_id": item.cv_artifact_id,
        "example_letter_artifact_id": item.example_letter_artifact_id,
        "retry_count": 0,
        "max_retries": legacy_config.MAX_RETRIES,
    }
    cover_result = _invoke_reusable_graph(
        cover_graph,
        cover_config,
        cover_input,
    )
    cover_letter = cover_result.get("cover_letter")
    if not cover_letter:
        raise ValueError("cover_letter_generation_failed")
    state["cover_letter"] = cover_letter
    email_graph = build_emailwriter_graph(checkpointer=checkpointer)
    email_input = {
        "messages": [],
        "company_name": result.get("company_name"),
        "contact_name": result.get("contact_name"),
        "job_role": result.get("job_role"),
        "research_summary": result.get("research_summary"),
        "cover_letter": cover_letter,
        "candidate_name": item.candidate_name,
        "candidate_profile": profile,
        "research_run_id": run.id,
        "retry_count": 0,
        "max_retries": legacy_config.MAX_RETRIES,
    }
    email_result = _invoke_reusable_graph(
        email_graph,
        {"configurable": {"thread_id": f"{run.thread_id}_email_{run.attempt}"}},
        email_input,
    )
    email_body = email_result.get("email_body")
    if not email_body:
        raise ValueError("email_generation_failed")
    subject = emaildrafter({
        "company_name": result.get("company_name"),
        "job_role": result.get("job_role"),
        "candidate_name": item.candidate_name,
        "research_run_id": run.id,
    }).get("subject")
    if not subject:
        raise ValueError("email_subject_generation_failed")

    with factory.begin() as session:
        current_run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run.id))
        current_item = session.scalar(select(BatchItem).where(BatchItem.id == item.id))
        if current_run is None or current_item is None:
            raise ValueError("run_or_item_missing")
        _store_generated_draft(session, current_run, current_item, "cover_letter", cover_letter)
        _store_generated_draft(session, current_run, current_item, "email", email_body, subject=subject)
        current_item.status = "draft_review"
        current_run.status = "paused"
        current_run.phase = "draft_review"
        current_run.progress = 90
        current_run.updated_at = utc_now()
        update_batch_status(session, current_run.batch_id, current_run.owner_id)
        append_event(
            session, owner_id=current_run.owner_id, batch_id=current_run.batch_id,
            run_id=current_run.id, event_type="drafts_ready", phase="draft_review",
            status="paused", progress=90, item_id=current_item.id,
            detail="Cover letter and email drafts are ready for review.",
        )


def _store_generated_draft(session, run, item, artifact_type: str, content: str, subject=None) -> None:
    current = session.scalar(
        select(DraftVersion)
        .where(
            DraftVersion.owner_id == run.owner_id,
            DraftVersion.item_id == item.id,
            DraftVersion.artifact_type == artifact_type,
        )
        .order_by(DraftVersion.version.desc())
        .limit(1)
    )
    version = (current.version if current else 0) + 1
    session.add(DraftVersion(
        id=str(uuid.uuid4()),
        owner_id=run.owner_id,
        batch_id=run.batch_id,
        item_id=item.id,
        artifact_type=artifact_type,
        version=version,
        content=content,
        subject=subject,
        created_by="agent",
        created_at=utc_now(),
    ))


def _invoke_reusable_graph(graph, thread_config: dict[str, Any], initial_state: dict[str, Any]) -> dict[str, Any]:
    checkpoint = graph.get_state(thread_config)
    if checkpoint.next:
        return graph.invoke(None, thread_config)
    if checkpoint.values:
        return checkpoint.values
    return graph.invoke(initial_state, thread_config)


def _process_message(factory, message_id: str) -> None:
    now = utc_now()
    with factory.begin() as session:
        message = session.scalar(
            select(OutboxMessage)
            .where(OutboxMessage.id == message_id)
            .with_for_update()
        )
        if message is None or message.processed_at is not None:
            return
        if message.processing_at is not None:
            return
        message.processing_at = now
        payload = dict(message.payload)
        topic = message.topic

    with postgres_checkpointer(Settings.from_environment().database_url) as checkpointer:
        if topic == "research_item":
            _process_research(factory, payload, checkpointer)
        elif topic == "resume_research":
            _process_research_decision(factory, payload, checkpointer)
        elif topic == "draft_item":
            _process_drafts(factory, payload, checkpointer)
        else:
            raise ValueError("unsupported_outbox_topic")

    with factory.begin() as session:
        message = session.get(OutboxMessage, message_id)
        if message:
            message.processed_at = utc_now()
            message.processing_at = None


@celery_app.task(name="webapp.worker.dispatch_outbox", ignore_result=True)
def dispatch_outbox(limit: int = 50) -> int:
    _, factory = _database_factory()
    now = utc_now()
    with factory.begin() as session:
        query = (
            select(OutboxMessage)
            .where(
                (OutboxMessage.published_at.is_(None))
                | (OutboxMessage.published_at < now - timedelta(seconds=_LEASE_SECONDS)),
                OutboxMessage.processed_at.is_(None),
                OutboxMessage.processing_at.is_(None),
                OutboxMessage.available_at <= now,
                (OutboxMessage.claimed_at.is_(None))
                | (OutboxMessage.claimed_at < now - timedelta(seconds=_LEASE_SECONDS)),
            )
            .order_by(OutboxMessage.created_at)
            .limit(max(1, min(limit, 100)))
            .with_for_update(skip_locked=True)
        )
        messages = list(session.scalars(query))
        selected = []
        for message in messages:
            message.claimed_at = now
            message.attempts += 1
            selected.append((message.id, message.topic))

    published = 0
    for message_id, _topic in selected:
        try:
            celery_app.send_task(
                "webapp.worker.process_outbox_message",
                args=[message_id],
                task_id=message_id,
            )
            with factory.begin() as session:
                message = session.get(OutboxMessage, message_id)
                if message:
                    message.published_at = utc_now()
                    message.claimed_at = None
            published += 1
        except Exception:
            terminal_failure = False
            with factory.begin() as session:
                message = session.get(OutboxMessage, message_id)
                if message:
                    message.claimed_at = None
                    message.last_error_code = "broker_unavailable"
                    if (
                        message.processing_at is None
                        and message.attempts >= _MAX_PUBLISH_ATTEMPTS
                    ):
                        message.processed_at = utc_now()
                        terminal_failure = True
                    elif message.processing_at is None:
                        message.available_at = utc_now() + timedelta(
                            seconds=min(300, 2 ** message.attempts)
                        )
            if terminal_failure:
                _mark_failed(factory, message_id, "broker_unavailable")
            logger.warning("outbox_publish_failed message_id=%s", message_id)
    return published


def _safe_error_code(error: Exception) -> str:
    known_codes = {
        "private_artifact_missing",
        "private_artifact_invalid",
        "run_or_item_missing",
        "research_decision_mismatch",
        "research_checkpoint_unavailable",
        "research_not_approved",
        "cv_artifact_missing",
        "cover_letter_generation_failed",
        "email_generation_failed",
        "email_subject_generation_failed",
        "unsupported_outbox_topic",
    }
    message = str(error).lower()
    return next((code for code in known_codes if code in message), "workflow_failed")


def _mark_failed(factory, message_id: str, error_code: str) -> None:
    with factory.begin() as session:
        message = session.get(OutboxMessage, message_id)
        if message is None:
            return
        message.processed_at = utc_now()
        message.processing_at = None
        message.last_error_code = error_code
        payload = message.payload
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == payload.get("run_id")))
        item = session.scalar(select(BatchItem).where(BatchItem.id == payload.get("item_id")))
        if run is None or item is None:
            return
        run.status = "failed"
        run.safe_error_code = error_code
        run.finished_at = utc_now()
        run.updated_at = utc_now()
        item.status = "failed"
        item.failure_code = error_code
        item.updated_at = utc_now()
        update_batch_status(session, run.batch_id, run.owner_id)
        append_event(
            session, owner_id=run.owner_id, batch_id=run.batch_id, run_id=run.id,
            event_type="item_failed", phase=run.phase, status="failed",
            progress=run.progress, item_id=item.id,
            detail="This item failed; other batch results remain available.",
        )


@celery_app.task(
    name="webapp.worker.process_outbox_message",
    ignore_result=True,
)
def process_outbox_message(message_id: str) -> None:
    _, factory = _database_factory()
    try:
        _process_message(factory, message_id)
    except Exception as error:
        error_code = _safe_error_code(error)
        _mark_failed(factory, message_id, error_code)
        logger.warning("workflow_job_failed message_id=%s error_code=%s", message_id, error_code)
