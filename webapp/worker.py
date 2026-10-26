from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

import config as legacy_config
from agents.cover_writer import build_coverwriter_graph
from agents.email_drafter import emaildrafter
from agents.email_writer import build_emailwriter_graph
from agents.researcher import build_researcher_graph
from graph.state import OutreachState
from persistence import (
    ApplicationRun,
    ApprovalDecision,
    BatchItem,
    DraftVersion,
    Exclusion,
    OutboxMessage,
    PrivateArtifact,
    RunEvent,
    get_exclusions,
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
        if phase == "research" and item.mode == "speculative":
            item.research_result = {
                **(item.research_result or {}),
                "search_attempt": 0,
                "search_budget": legacy_config.SPECULATIVE_SEARCH_BUDGET,
                "rejected_candidates": [],
            }
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
        rejected_candidates = values.get("rejected_candidates") or []
        is_accepted = bool(paused and values.get("company_name"))
        accepted_count = 1 if is_accepted else 0
        evaluated_count = len(rejected_candidates) + (1 if is_accepted else 0)
        discovered_count = max(evaluated_count, int(values.get("search_attempt", 1)))
        result = {
            "company_name": values.get("company_name"),
            "company_domain": values.get("company_domain"),
            "contact_name": values.get("contact_name"),
            "contact_email": values.get("contact_email"),
            "job_role": values.get("job_role"),
            "job_description": values.get("job_description"),
            "research_summary": values.get("research_summary"),
            "role_fit": "suitable" if values.get("research_sufficient") else "not_suitable",
            "rejected_candidates": rejected_candidates,
            "candidates_discovered": discovered_count,
            "candidates_evaluated": evaluated_count,
            "candidates_accepted": accepted_count,
            "search_attempt": values.get("search_attempt", 1),
            "search_budget": values.get("search_budget", legacy_config.SPECULATIVE_SEARCH_BUDGET),
            "evaluator_feedback": values.get("evaluator_feedback"),
            "eligibility_reason": values.get("eligibility_reason") or values.get("evaluator_feedback"),
            "company_relevant": values.get("company_relevant", True),
            "role_suitable": values.get("role_suitable", True),
            "seniority_match": values.get("seniority_match", True),
            "experience_required": values.get("experience_required"),
        }
        item.research_result = result
        item.target_company = result["company_name"] or item.target_company
        item.target_domain = result["company_domain"] or item.target_domain
        item.target_role = result["job_role"] or item.target_role
        outcome = values.get("research_outcome")
        item.status = "research_review" if paused else (
            "ineligible" if outcome in {
                "no_matching_role",
                "no_usable_contact",
                "search_exhausted",
            } else "failed"
        )
        item.failure_code = (
            None if paused else (
                "search_exhausted" if outcome == "search_exhausted"
                else "no_matching_role" if outcome == "no_matching_role"
                else "no_usable_contact" if outcome == "no_usable_contact"
                else "research_failed"
            )
        )
        run.status = "paused" if paused else ("completed" if item.status == "ineligible" else "failed")
        run.phase = "research_review"
        run.progress = 45 if paused else 100
        run.safe_error_code = item.failure_code if item.status == "failed" else None
        run.updated_at = utc_now()
        run.finished_at = None if paused else utc_now()
        update_batch_status(session, run.batch_id, run.owner_id)
        rejected_candidates = result["rejected_candidates"]
        for index, candidate in enumerate(rejected_candidates, start=1):
            detail = _opportunity_skip_detail(candidate)
            exists = session.scalar(
                select(RunEvent.id).where(
                    RunEvent.run_id == run.id,
                    RunEvent.event_type == "opportunity_skipped",
                    RunEvent.safe_detail == detail,
                )
            )
            if exists:
                continue
            append_event(
                session,
                owner_id=run.owner_id,
                batch_id=run.batch_id,
                run_id=run.id,
                event_type="opportunity_skipped",
                phase="research",
                status="running",
                progress=min(44, max(5, int(40 * index / max(len(rejected_candidates), 1)))),
                item_id=item.id,
                detail=detail,
            )
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
            detail=(
                "Suitable opportunities found: Research result is ready for review."
                if paused
                else (
                    (
                        f"Search completed but no suitable opportunities found after evaluating {evaluated_count} candidates across "
                        f"{result['search_attempt']} of {result['search_budget']} searches (accepted: 0). "
                        f"Rejection summary: {'; '.join(dict.fromkeys(c.get('reason') for c in rejected_candidates if c.get('reason'))) or 'Criteria not met'}."
                    )
                    if outcome == "search_exhausted"
                    else (
                        f"Previously excluded opportunity skipped ({result.get('company_name') or 'target'})."
                        if "excluded" in str(values.get("evaluator_feedback", "")).lower()
                        else f"Research finished without a reviewable result ({item.failure_code})."
                    )
                )
            ),
        )


def _research_inputs(
    item: BatchItem,
    run: ApplicationRun,
    candidate_profile: str,
    sibling_exclusions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    existing_result = item.research_result or {}
    existing_rejected = list(existing_result.get("rejected_candidates") or [])

    # Load persisted exclusions for this owner
    owner_exclusions = get_exclusions(run.owner_id) if run.owner_id else []
    existing_keys = {
        (
            " ".join((cand.get("company_name") or "").casefold().split()),
            " ".join((cand.get("job_role") or "").casefold().split()),
        )
        for cand in existing_rejected
    }
    for exc in owner_exclusions:
        c_name = exc.get("company_name") or ""
        c_domain = exc.get("company_domain") or ""
        k = (" ".join(c_name.casefold().split()), "")
        if k not in existing_keys and (c_name or c_domain):
            reason_text = exc.get("rejection_reason") or "Previously excluded"
            if not reason_text.startswith("Previously excluded") and not reason_text.startswith("Rejected by"):
                reason_text = f"Previously excluded: {reason_text}"
            existing_rejected.append({
                "status": "rejected",
                "company_name": c_name,
                "company_domain": c_domain,
                "job_role": "",
                "reason": reason_text,
                "category": "Previously excluded",
                "company_excluded": True,
            })
            existing_keys.add(k)

    if sibling_exclusions:
        for entry in sibling_exclusions:
            ent_key = (
                " ".join((entry.get("company_name") or "").casefold().split()),
                " ".join((entry.get("job_role") or "").casefold().split()),
            )
            if ent_key not in existing_keys and (entry.get("company_name") or entry.get("company_domain")):
                existing_rejected.append(entry)
                existing_keys.add(ent_key)
    current_attempt = int(existing_result.get("search_attempt") or 0)
    configured_budget = int(
        os.getenv("SPECULATIVE_SEARCH_BUDGET")
        or getattr(legacy_config, "SPECULATIVE_SEARCH_BUDGET", 10)
    )
    item_budget = getattr(item, "search_budget", None) or existing_result.get("search_budget")
    effective_budget = int(item_budget or configured_budget)
    return {
        "messages": [],
        "seed_query": (
            (item.target_company or item.target_domain or "")
            if item.mode == "open_role"
            else None
        ),
        "candidate_profile": item.candidate_profile or candidate_profile,
        "mode": item.mode,
        "target_seniority": item.target_seniority,
        "target_domain_keywords": item.target_domain_keywords,
        "location_preference": item.location_preference,
        "retry_count": 0,
        "max_retries": legacy_config.MAX_RETRIES,
        "search_attempt": current_attempt,
        "search_budget": effective_budget,
        "retry_counted": False,
        "rejected_candidates": existing_rejected,
        "research_run_id": run.id,
        "owner_id": run.owner_id,
        "contact_attempts": 0,
        "research_outcome": None,
        "research_error": None,
        "research_cache_hit": False,
    }


def _opportunity_skip_detail(candidate: dict[str, Any]) -> str:
    name = candidate.get("company_name") or candidate.get("company_domain") or "Unknown company"
    role = candidate.get("job_role") or "role not identified"
    category = candidate.get("category")
    reason = candidate.get("reason") or "not suitable"
    if category == "Previously excluded" or reason.startswith("Previously excluded"):
        return f"Previously excluded opportunity skipped: {name}."[:500]
    return f"Skipped {name} — {role}: {reason}."[:500]


def _persist_research_progress(
    factory,
    run_id: str,
    item_id: str,
    *,
    search_attempt: int | None = None,
    search_budget: int | None = None,
    rejected_candidates: list[dict[str, Any]] | None = None,
) -> None:
    with factory.begin() as session:
        run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run_id))
        item = session.scalar(select(BatchItem).where(BatchItem.id == item_id))
        if run is None or item is None or run.owner_id != item.owner_id:
            raise ValueError("run_or_item_missing")
        result = dict(item.research_result or {})
        if search_attempt is not None:
            result["search_attempt"] = search_attempt
        if search_budget is not None:
            result["search_budget"] = search_budget
        if rejected_candidates is not None:
            result["rejected_candidates"] = rejected_candidates
        item.research_result = result
        item.updated_at = utc_now()

        if search_attempt is not None:
            detail = f"Searching opportunity {search_attempt} of {result.get('search_budget', search_budget or 0)}."
            exists = session.scalar(
                select(RunEvent.id).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "search_attempt_started",
                    RunEvent.safe_detail == detail,
                )
            )
            if not exists:
                progress = min(
                    44,
                    max(5, int(5 + 35 * search_attempt / max(result.get("search_budget", 1), 1))),
                )
                run.progress = max(run.progress, progress)
                append_event(
                    session,
                    owner_id=run.owner_id,
                    batch_id=run.batch_id,
                    run_id=run.id,
                    event_type="search_attempt_started",
                    phase="research",
                    status="running",
                    progress=run.progress,
                    item_id=item.id,
                    detail=detail,
                )

        for candidate in rejected_candidates or []:
            detail = _opportunity_skip_detail(candidate)
            exists = session.scalar(
                select(RunEvent.id).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "opportunity_skipped",
                    RunEvent.safe_detail == detail,
                )
            )
            if not exists:
                append_event(
                    session,
                    owner_id=run.owner_id,
                    batch_id=run.batch_id,
                    run_id=run.id,
                    event_type="opportunity_skipped",
                    phase="research",
                    status="running",
                    progress=run.progress,
                    item_id=item.id,
                    detail=detail,
                )


def _invoke_research_graph(graph, value, thread_config, factory, run_id: str, item_id: str) -> None:
    stream = getattr(graph, "stream", None)
    if not callable(stream):
        graph.invoke(value, thread_config)
        return
    for update in stream(value, thread_config, stream_mode="updates"):
        if not isinstance(update, dict):
            continue
        for node_name, node_update in update.items():
            if not isinstance(node_update, dict):
                continue
            if node_name == "inputpromptformater":
                _persist_research_progress(
                    factory,
                    run_id,
                    item_id,
                    search_attempt=node_update.get("search_attempt"),
                    search_budget=node_update.get("search_budget"),
                )
            elif node_name == "retry_company":
                _persist_research_progress(
                    factory,
                    run_id,
                    item_id,
                    search_attempt=node_update.get("search_attempt"),
                    search_budget=node_update.get("search_budget"),
                    rejected_candidates=node_update.get("rejected_candidates"),
                )


def _process_research(factory, payload: dict[str, Any], checkpointer) -> None:
    run, item = _state_snapshot(factory, payload["run_id"], payload["item_id"])
    if item.status in {"research_review", "rejected", "ineligible", "draft_review", "completed"}:
        return
    graph = build_researcher_graph(checkpointer=checkpointer)
    thread_id = payload.get("thread_id") or f"{run.thread_id}:research"
    thread_config = {"configurable": {"thread_id": thread_id}}
    checkpoint = graph.get_state(thread_config)
    _mark_running(factory, run.id, item.id, "research", 5)
    if checkpoint.next and checkpoint.values.get("research_outcome") == "awaiting_review":
        values = checkpoint.values
    elif checkpoint.next:
        _invoke_research_graph(graph, None, thread_config, factory, run.id, item.id)
        checkpoint = graph.get_state(thread_config)
        values = checkpoint.values
    elif checkpoint.values:
        values = checkpoint.values
    else:
        settings = Settings.from_environment()
        with factory() as session:
            raw_cv = _private_text(session, settings, run.owner_id, item.cv_artifact_id)
            profile = generate_candidate_profile(raw_cv) if raw_cv else ""
            sibling_items = session.scalars(
                select(BatchItem).where(
                    BatchItem.batch_id == item.batch_id,
                    BatchItem.owner_id == run.owner_id,
                    BatchItem.id != item.id,
                )
            ).all()
            sibling_exclusions: list[dict[str, str]] = []
            for sib in sibling_items:
                sib_res = sib.research_result or {}
                sib_comp = sib.target_company or sib_res.get("company_name")
                sib_domain = sib.target_domain or sib_res.get("company_domain")
                sib_role = sib.target_role or sib_res.get("job_role")
                if sib_comp or sib_domain:
                    sibling_exclusions.append({
                        "status": "rejected",
                        "company_name": sib_comp or "",
                        "company_domain": sib_domain or "",
                        "job_role": sib_role or "",
                        "reason": "Opportunity already targeted or discovered for another candidate in this batch.",
                    })
            owner_exclusions = session.scalars(
                select(Exclusion).where(Exclusion.owner_id == run.owner_id)
            ).all()
            for exc in owner_exclusions:
                key = exc.normalized_key or ""
                comp_name = key.replace("name:", "") if key.startswith("name:") else None
                comp_domain = key.replace("domain:", "") if key.startswith("domain:") else None
                sibling_exclusions.append({
                    "status": "rejected",
                    "company_name": comp_name or "",
                    "company_domain": comp_domain or "",
                    "job_role": "",
                    "reason": exc.rejection_reason or "Account exclusion.",
                })
        _invoke_research_graph(
            graph,
            _research_inputs(item, run, profile, sibling_exclusions=sibling_exclusions),
            thread_config,
            factory,
            run.id,
            item.id,
        )
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
    thread_id = payload.get("thread_id") or f"{run.thread_id}:research"
    thread_config = {"configurable": {"thread_id": thread_id}}
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
                        rejection_reason="Rejected by user: Candidate rejected by user.",
                        created_at=utc_now(),
                    ))

            # Maintain rejected candidate history in item.research_result
            res = dict(item.research_result or {})
            rejected_list = list(res.get("rejected_candidates") or [])
            if company or res.get("job_role"):
                rejected_list.append({
                    "status": "rejected",
                    "company_name": company or "",
                    "company_domain": domain or "",
                    "job_role": res.get("job_role") or "",
                    "reason": "Rejected by user: User explicitly rejected this candidate opportunity.",
                    "category": "Rejected by user",
                    "company_excluded": True,
                })
            res["rejected_candidates"] = rejected_list
            item.research_result = res

            current_attempt = int(res.get("search_attempt") or 1)
            budget = int(
                res.get("search_budget")
                or os.getenv("SPECULATIVE_SEARCH_BUDGET")
                or getattr(legacy_config, "MAX_RESEARCH_ATTEMPTS", 5)
            )
            # In speculative discovery mode, check if we can continue searching for another opportunity
            if item.mode == "speculative" and current_attempt < budget:
                item.status = "queued"
                item.research_decision = None
                item.failure_code = None
                run.attempt += 1
                run.status = "queued"
                run.phase = "research"
                run.progress = 10
                run.finished_at = None
                # Enqueue research_item to continue searching
                continuation_dedupe = f"research-continue:{run.id}:{run.attempt}"
                session.add(OutboxMessage(
                    id=str(uuid.uuid4()),
                    topic="research_item",
                    dedupe_key=continuation_dedupe,
                    payload={
                        "run_id": run.id,
                        "batch_id": run.batch_id,
                        "item_id": item.id,
                        "owner_id": run.owner_id,
                        "thread_id": f"{run.thread_id}:{run.attempt}",
                    },
                    available_at=utc_now(),
                    attempts=0,
                    created_at=utc_now(),
                ))
                event_type = "research_rejection_continuation_queued"
            else:
                item.status = "rejected" if item.mode != "speculative" else "ineligible"
                item.failure_code = "search_exhausted" if item.mode == "speculative" else None
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
            detail=(
                "Explicit user rejection: Research decision applied to the saved run."
                if decision == "rejected"
                else "Research decision applied to the saved run."
            ),
        )


def _process_drafts(factory, payload: dict[str, Any], checkpointer) -> None:
    run, item = _state_snapshot(factory, payload["run_id"], payload["item_id"])
    if item.research_decision != "approved":
        raise ValueError("research_not_approved")
    if item.status in {"draft_review", "completed"}:
        return
    if not item.cv_artifact_id:
        raise ValueError("cv_artifact_missing")

    rejected_artifact = payload.get("rejected_artifact")

    with factory() as check_session:
        latest_cover = check_session.scalar(
            select(DraftVersion).where(
                DraftVersion.batch_id == run.batch_id,
                DraftVersion.item_id == item.id,
                DraftVersion.owner_id == run.owner_id,
                DraftVersion.artifact_type == "cover_letter",
            ).order_by(DraftVersion.version.desc()).limit(1)
        )
        latest_email = check_session.scalar(
            select(DraftVersion).where(
                DraftVersion.batch_id == run.batch_id,
                DraftVersion.item_id == item.id,
                DraftVersion.owner_id == run.owner_id,
                DraftVersion.artifact_type == "email",
            ).order_by(DraftVersion.version.desc()).limit(1)
        )
        cover_approval = check_session.scalar(
            select(ApprovalDecision).where(
                ApprovalDecision.owner_id == run.owner_id,
                ApprovalDecision.item_id == item.id,
                ApprovalDecision.artifact_type == "cover_letter",
                ApprovalDecision.draft_version_id == latest_cover.id,
            )
        ) if latest_cover else None

    if rejected_artifact == "email":
        should_regenerate_cover = False
    elif rejected_artifact == "cover_letter":
        should_regenerate_cover = True
    elif latest_cover is None or latest_email is None:
        should_regenerate_cover = True
    elif cover_approval and cover_approval.decision == "approved":
        should_regenerate_cover = False
    else:
        should_regenerate_cover = True

    already_drafted = False
    if rejected_artifact == "email":
        if latest_email and latest_email.version >= run.attempt:
            already_drafted = True
    elif rejected_artifact == "cover_letter":
        if latest_cover and latest_email and latest_cover.version >= run.attempt and latest_email.version >= run.attempt:
            already_drafted = True
    else:
        max_draft_version = check_session.scalar(
            select(func.max(DraftVersion.version)).where(
                DraftVersion.batch_id == run.batch_id,
                DraftVersion.item_id == item.id,
                DraftVersion.owner_id == run.owner_id,
            )
        )
        if max_draft_version is not None and max_draft_version >= run.attempt:
            already_drafted = True

    if already_drafted:
        with factory.begin() as update_session:
            cur_item = update_session.get(BatchItem, item.id)
            cur_run = update_session.get(ApplicationRun, run.id)
            if cur_item and cur_item.status in {"queued", "drafting"}:
                cur_item.status = "draft_review"
            if cur_run and cur_run.status in {"queued", "running"}:
                cur_run.status = "paused"
                cur_run.phase = "draft_review"
            update_batch_status(update_session, run.batch_id, run.owner_id)
        return

    settings = Settings.from_environment()
    with factory() as session:
        raw_cv = _private_text(session, settings, run.owner_id, item.cv_artifact_id)
        profile = generate_candidate_profile(raw_cv)
        result = item.research_result or {}

    def load_artifact(artifact_id: str) -> str:
        with factory() as artifact_session:
            return _private_text(artifact_session, settings, run.owner_id, artifact_id)

    _mark_running(factory, run.id, item.id, "drafting", 55)

    if should_regenerate_cover:
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
            "candidate_name": item.candidate_name,
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

        with factory.begin() as session:
            cur_run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run.id))
            cur_item = session.scalar(select(BatchItem).where(BatchItem.id == item.id))
            if cur_run is not None and cur_item is not None:
                _store_generated_draft(session, cur_run, cur_item, "cover_letter", cover_letter)
                cur_run.progress = 70
                cur_run.updated_at = utc_now()
                append_event(
                    session,
                    owner_id=cur_run.owner_id,
                    batch_id=cur_run.batch_id,
                    run_id=cur_run.id,
                    event_type="cover_letter_saved",
                    phase="drafting",
                    status="running",
                    progress=70,
                    item_id=cur_item.id,
                    detail="Cover letter draft generated and persisted.",
                )
    else:
        if latest_cover is None or not latest_cover.content:
            raise ValueError("cover_letter_missing")
        cover_letter = latest_cover.content
        with factory.begin() as session:
            cur_run = session.scalar(select(ApplicationRun).where(ApplicationRun.id == run.id))
            if cur_run:
                cur_run.progress = 70
                cur_run.updated_at = utc_now()

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
            detail=(
                "Cover letter and email drafts are ready for review."
                if should_regenerate_cover
                else "Email draft regenerated and ready for review."
            ),
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
    try:
        _, factory = _database_factory()
    except RuntimeError as error:
        settings = Settings.from_environment()
        if not settings.database_url.startswith("sqlite:") or "require postgresql" not in str(error).lower():
            raise
        factory = session_factory(settings)
        with factory() as session:
            pending_ids = list(session.scalars(
                select(OutboxMessage.id).where(
                    OutboxMessage.processed_at.is_(None),
                    OutboxMessage.processing_at.is_(None),
                )
            ))
        for message_id in pending_ids:
            _mark_failed(factory, message_id, "worker_requires_postgresql")
        return 0
    now = utc_now()
    stale_cutoff = now - timedelta(seconds=_LEASE_SECONDS)
    with factory() as session:
        stale_message_ids = list(session.scalars(
            select(OutboxMessage.id).where(
                OutboxMessage.processed_at.is_(None),
                OutboxMessage.processing_at < stale_cutoff,
            )
        ))
    for message_id in stale_message_ids:
        _mark_failed(factory, message_id, "worker_interrupted")

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
        "checkpointer_initialization_failed",
        "worker_interrupted",
        "ai_provider_authentication_failed",
        "ai_provider_quota_exceeded",
        "ai_provider_timeout",
        "ai_provider_error",
    }
    message = str(error).lower()
    if "require postgresql" in message:
        return "worker_requires_postgresql"
    if "langgraph_aes_key" in message:
        return "worker_configuration_invalid"
    if "checkpoint" in message:
        return "research_checkpoint_unavailable"
    if isinstance(error, (ValueError, PermissionError)):
        for code in known_codes:
            if code in message:
                return code
    if "api key" in message or "apikey" in message or "unauthenticated" in message or "invalid_api_key" in message:
        return "ai_provider_authentication_failed"
    if "rate limit" in message or "quota" in message or "resourceexhausted" in message or "429" in message:
        return "ai_provider_quota_exceeded"
    if "timeout" in message or "timed out" in message:
        return "ai_provider_timeout"
    if "gemini" in message or "googleapi" in message:
        return "ai_provider_error"
    return next((code for code in known_codes if code in message), "workflow_failed")


def _mark_failed(factory, message_id: str, error_code: str) -> None:
    with factory.begin() as session:
        message = session.get(OutboxMessage, message_id)
        if message is None:
            return
        message.processed_at = utc_now()
        message.processing_at = None
        message.claimed_at = None
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

        if "postgresql" in error_code or "configuration" in error_code or "checkpointer" in error_code:
            failure_detail = f"Worker/database configuration failure: {error_code}."
        elif "provider" in error_code or "quota" in error_code or "auth" in error_code or "timeout" in error_code:
            failure_detail = f"Provider failure: {error_code}."
        elif "exhausted" in error_code or "no_matching" in error_code:
            failure_detail = "Search completed but no suitable opportunities found."
        else:
            failure_detail = f"This item failed ({error_code}); other batch results remain available. Explicit retry is available."

        append_event(
            session, owner_id=run.owner_id, batch_id=run.batch_id, run_id=run.id,
            event_type="item_failed", phase=run.phase, status="failed",
            progress=run.progress, item_id=item.id,
            detail=failure_detail,
        )


@celery_app.task(
    name="webapp.worker.process_outbox_message",
    ignore_result=True,
    bind=True,
)
def process_outbox_message(self, message_id: str) -> None:
    settings = Settings.from_environment()
    factory = session_factory(settings)
    task_id = getattr(self.request, "id", None) if hasattr(self, "request") else None
    try:
        _, factory = _database_factory()
        _process_message(factory, message_id)
    except Exception as error:
        error_code = _safe_error_code(error)
        _mark_failed(factory, message_id, error_code)
        with factory() as session:
            msg = session.get(OutboxMessage, message_id)
            payload = msg.payload if msg else {}
        logger.error(
            "workflow_job_failed message_id=%s task_id=%s topic=%s run_id=%s item_id=%s batch_id=%s error_code=%s exception=%s: %s",
            message_id,
            task_id,
            payload.get("topic"),
            payload.get("run_id"),
            payload.get("item_id"),
            payload.get("batch_id"),
            error_code,
            type(error).__name__,
            error,
            exc_info=True,
        )
