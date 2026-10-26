from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence import (
    ApprovalDecision,
    AppUser,
    ApplicationRun,
    BatchItem,
    DraftVersion,
    OutboxMessage,
    OutreachBatch,
    PrivateArtifact,
    RunEvent,
    utc_now,
)
from webapp.auth import Principal
from webapp.schemas import BatchCreateRequest
from webapp.settings import Settings
from utils.cv_parser import extract_text_from_cv, extract_candidate_name, generate_candidate_profile


def _user(session: Session, principal: Principal) -> AppUser:
    user = session.get(AppUser, principal.user_id)
    if user is None:
        user = session.scalar(
            select(AppUser).where(
                AppUser.identity_provider == principal.identity_provider,
                AppUser.external_subject == principal.subject,
            )
        )
        if user is None:
            user = AppUser(
                id=principal.user_id,
                identity_provider=principal.identity_provider,
                external_subject=principal.subject,
                email=principal.email,
            )
            session.add(user)
            session.flush()
        elif user.id != principal.user_id:
            raise HTTPException(status_code=401, detail="Authentication identity is inconsistent.")
    elif (
        user.external_subject != principal.subject
        or user.identity_provider != principal.identity_provider
    ):
        raise HTTPException(status_code=401, detail="Authentication identity is inconsistent.")
    return user


def append_event(
    session: Session,
    *,
    owner_id: str,
    batch_id: str,
    run_id: str,
    event_type: str,
    phase: str | None = None,
    status: str | None = None,
    progress: int | None = None,
    item_id: str | None = None,
    detail: str | None = None,
) -> None:
    session.scalar(
        select(OutreachBatch.id)
        .where(OutreachBatch.id == batch_id, OutreachBatch.owner_id == owner_id)
        .with_for_update()
    )
    previous = session.scalar(
        select(func.max(RunEvent.sequence)).where(RunEvent.batch_id == batch_id)
    )
    session.add(
        RunEvent(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            batch_id=batch_id,
            run_id=run_id,
            sequence=(previous or 0) + 1,
            event_type=event_type,
            phase=phase,
            status=status,
            progress=progress,
            item_id=item_id,
            safe_detail=detail,
        )
    )


def owned_batch(session: Session, owner_id: str, batch_id: str) -> OutreachBatch:
    batch = session.scalar(
        select(OutreachBatch).where(
            OutreachBatch.id == batch_id,
            OutreachBatch.owner_id == owner_id,
        )
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return batch


def owned_item(
    session: Session, owner_id: str, batch_id: str, item_id: str
) -> BatchItem:
    item = session.scalar(
        select(BatchItem).where(
            BatchItem.id == item_id,
            BatchItem.batch_id == batch_id,
            BatchItem.owner_id == owner_id,
        ).with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Batch item not found.")
    return item


def latest_draft(
    session: Session, owner_id: str, item_id: str, artifact_type: str
) -> DraftVersion | None:
    return session.scalar(
        select(DraftVersion)
        .where(
            DraftVersion.owner_id == owner_id,
            DraftVersion.item_id == item_id,
            DraftVersion.artifact_type == artifact_type,
        )
        .order_by(DraftVersion.version.desc())
        .limit(1)
    )


def create_batch(session: Session, principal: Principal, request: BatchCreateRequest, max_items: int) -> OutreachBatch:
    if len(request.items) > max_items:
        raise HTTPException(status_code=422, detail=f"A batch may contain at most {max_items} items.")
    user = _user(session, principal)
    for artifact_id, artifact_type in (
        (request.cv_artifact_id, "cv"),
        (request.example_letter_artifact_id, "example_letter"),
    ):
        if artifact_id and not session.scalar(
            select(PrivateArtifact.id).where(
                PrivateArtifact.id == artifact_id,
                PrivateArtifact.owner_id == user.id,
                PrivateArtifact.artifact_type == artifact_type,
            )
        ):
            raise HTTPException(status_code=404, detail="Private artifact not found.")

    resolved_candidate_name = (request.candidate_name or "").strip() or None
    resolved_candidate_profile = (request.candidate_profile or "").strip() or None

    if request.cv_artifact_id and (not resolved_candidate_name or not resolved_candidate_profile):
        cv_artifact = session.scalar(
            select(PrivateArtifact).where(
                PrivateArtifact.id == request.cv_artifact_id,
                PrivateArtifact.owner_id == user.id,
            )
        )
        if cv_artifact:
            settings = Settings.from_environment()
            cv_path = (settings.private_artifact_dir / cv_artifact.storage_key).resolve()
            if cv_path.is_file():
                try:
                    cv_text = extract_text_from_cv(cv_path, filename=cv_artifact.filename)
                    if cv_text:
                        if not resolved_candidate_name:
                            extracted = extract_candidate_name(cv_text)
                            if extracted and extracted != "Candidate":
                                resolved_candidate_name = extracted
                        if not resolved_candidate_profile:
                            try:
                                resolved_candidate_profile = generate_candidate_profile(cv_text)
                            except Exception:
                                resolved_candidate_profile = cv_text[:1200]
                except Exception:
                    pass
    if not resolved_candidate_name:
        resolved_candidate_name = "Candidate"

    now = utc_now()
    batch = OutreachBatch(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        label=request.label,
        status="queued",
        created_at=now,
        updated_at=now,
    )
    session.add(batch)
    session.flush()

    for target in request.items:
        item_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        thread_id = f"web_{run_id}"
        item = BatchItem(
            id=item_id,
            batch_id=batch.id,
            owner_id=user.id,
            mode=request.mode,
            target_company=target.company_name,
            target_domain=target.domain,
            target_role=target.role,
            target_description=target.job_description,
            candidate_name=resolved_candidate_name,
            candidate_profile=resolved_candidate_profile,
            target_seniority=request.target_seniority,
            target_domain_keywords=request.target_domain_keywords,
            location_preference=request.location_preference,
            cv_artifact_id=request.cv_artifact_id,
            example_letter_artifact_id=request.example_letter_artifact_id,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        run = ApplicationRun(
            id=run_id,
            owner_id=user.id,
            batch_id=batch.id,
            item_id=item_id,
            thread_id=thread_id,
            status="queued",
            phase="research",
            progress=0,
            attempt=0,
            created_at=now,
            updated_at=now,
        )
        session.add(item)
        session.flush()
        session.add(run)
        session.add(
            OutboxMessage(
                id=str(uuid.uuid4()),
                topic="research_item",
                dedupe_key=f"research:{run_id}:0",
                payload={
                    "run_id": run_id,
                    "batch_id": batch.id,
                    "item_id": item_id,
                    "owner_id": user.id,
                    "thread_id": thread_id,
                },
                available_at=now,
                attempts=0,
                created_at=now,
            )
        )
        append_event(
            session,
            owner_id=user.id,
            batch_id=batch.id,
            run_id=run_id,
            event_type="queued",
            phase="research",
            status="queued",
            progress=0,
            item_id=item_id,
            detail="Research queued.",
        )
    session.flush()
    return batch


def update_batch_status(session: Session, batch_id: str, owner_id: str) -> None:
    items = list(
        session.scalars(
            select(BatchItem).where(
                BatchItem.batch_id == batch_id,
                BatchItem.owner_id == owner_id,
            )
        )
    )
    if not items:
        status = "queued"
    elif any(item.status == "research_review" for item in items):
        status = "research_review"
    elif any(item.status == "draft_review" for item in items):
        status = "draft_review"
    elif any(
        item.status == "researching"
        or (item.status == "queued" and item.research_decision != "approved")
        for item in items
    ):
        status = "researching"
    elif any(
        item.status == "drafting"
        or (item.status == "queued" and item.research_decision == "approved")
        for item in items
    ):
        status = "drafting"
    elif any(item.status == "failed" for item in items):
        status = "failed"
    elif all(item.status in {"rejected", "ineligible", "completed"} for item in items):
        status = "completed"
    else:
        status = "completed"
    batch = session.get(OutreachBatch, batch_id)
    if batch and batch.owner_id == owner_id:
        batch.status = status
        batch.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
