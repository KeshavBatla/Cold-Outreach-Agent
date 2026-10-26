from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence import (
    ApprovalDecision,
    AppUser,
    ApplicationRun,
    BatchItem,
    DeliveryRecord,
    DraftVersion,
    OutboxMessage,
    OutreachBatch,
    PrivateArtifact,
    RunEvent,
    utc_now,
)
from webapp.auth import Principal, get_current_principal
from webapp.database import get_session
from utils.helpers import find_unresolved_placeholders
from webapp.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    ArtifactResponse,
    BatchCreateRequest,
    BatchItemResponse,
    BatchListResponse,
    BatchResponse,
    CombinedApprovalRequest,
    DraftDocument,
    DraftEditRequest,
    DraftGenerationRequest,
    DraftResponse,
    HealthResponse,
    ResearchCard,
    ResearchDecisionRequest,
    RunEventResponse,
    RunStatusResponse,
)
from webapp.services import (
    append_event,
    create_batch,
    latest_draft,
    owned_batch,
    owned_item,
    update_batch_status,
)
from webapp.settings import Settings


def build_api_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["batches"])
    session_dependency = Depends(get_session)
    principal_dependency = Depends(get_current_principal)

    @router.post("/artifacts", response_model=ArtifactResponse, status_code=status.HTTP_200_OK)
    async def upload_private_artifact(
        request: Request,
        file: UploadFile = File(...),
        artifact_type: Literal["cv", "example_letter"] = "cv",
        principal: Principal = principal_dependency,
        session: Session = session_dependency,
    ):
        settings: Settings = request.app.state.settings
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt"}:
            raise HTTPException(status_code=415, detail="Upload a PDF, DOCX, or TXT file.")
        max_bytes = 10 * 1024 * 1024
        data = await file.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            raise HTTPException(status_code=413, detail="Artifact must be between 1 byte and 10 MiB.")
        if suffix == ".txt" and file.content_type not in {"text/plain", None}:
            raise HTTPException(status_code=415, detail="Text artifact content type is invalid.")

        artifact_id = str(uuid.uuid4())
        destination: Path | None = None
        try:
            with session.begin():
                user = session.get(AppUser, principal.user_id)
                if user is None:
                    user = AppUser(
                        id=principal.user_id,
                        identity_provider=principal.identity_provider,
                        external_subject=principal.subject,
                        email=principal.email,
                    )
                    session.add(user)
                    session.flush()
                elif (
                    user.external_subject != principal.subject
                    or user.identity_provider != principal.identity_provider
                ):
                    raise HTTPException(status_code=401, detail="Authentication identity is inconsistent.")
                artifact_root = settings.private_artifact_dir.resolve()
                owner_folder = hashlib.sha256(user.id.encode("utf-8")).hexdigest()
                owner_dir = (artifact_root / owner_folder).resolve()
                if not owner_dir.is_relative_to(artifact_root):
                    raise HTTPException(status_code=401, detail="Authentication identity is invalid.")
                owner_dir.mkdir(parents=True, exist_ok=True)
                storage_key = f"{owner_folder}/{artifact_id}{suffix}"
                destination = (artifact_root / storage_key).resolve()
                if not destination.is_relative_to(artifact_root):
                    raise HTTPException(status_code=500, detail="Private storage path is invalid.")
                destination.write_bytes(data)
                artifact = PrivateArtifact(
                    id=artifact_id,
                    owner_id=user.id,
                    batch_id=None,
                    item_id=None,
                    artifact_type=artifact_type,
                    storage_key=storage_key,
                    filename=Path(file.filename or "upload").name[:255],
                    media_type=file.content_type,
                    byte_size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    created_at=utc_now(),
                )
                session.add(artifact)
                session.flush()
                response = ArtifactResponse(
                    id=artifact.id,
                    artifact_type=artifact.artifact_type,
                    filename=artifact.filename,
                    media_type=artifact.media_type,
                    byte_size=artifact.byte_size,
                    created_at=artifact.created_at,
                )
            return response
        except Exception:
            if destination and destination.exists():
                destination.unlink()
            raise
        finally:
            await file.close()

    @router.post("/batches", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
    def create_batch_endpoint(
        request: Request,
        payload: BatchCreateRequest,
        principal: Principal = principal_dependency,
        session: Session = session_dependency,
    ):
        settings: Settings = request.app.state.settings
        with session.begin():
            batch = create_batch(session, principal, payload, settings.max_batch_items)
            session.flush()
            return _batch_response(session, batch)

    @router.get("/batches", response_model=BatchListResponse)
    def list_batches(
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(status_code=422, detail="Invalid pagination.")
        query = select(OutreachBatch).where(OutreachBatch.owner_id == principal.user_id)
        if not include_archived:
            query = query.where(OutreachBatch.is_archived.is_(False))
        batches = list(
            session.scalars(
                query.order_by(OutreachBatch.created_at.desc(), OutreachBatch.id)
                .limit(limit)
                .offset(offset)
            )
        )
        return BatchListResponse(
            items=[_batch_response(session, batch) for batch in batches],
            limit=limit,
            offset=offset,
        )

    @router.post("/batches/{batch_id}/archive", response_model=BatchResponse)
    def archive_batch(
        batch_id: str,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        with session.begin():
            batch = owned_batch(session, principal.user_id, batch_id)
            batch.is_archived = True
            batch.updated_at = utc_now()
            session.flush()
            return _batch_response(session, batch)

    @router.post("/batches/{batch_id}/unarchive", response_model=BatchResponse)
    def unarchive_batch(
        batch_id: str,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        with session.begin():
            batch = owned_batch(session, principal.user_id, batch_id)
            batch.is_archived = False
            batch.updated_at = utc_now()
            session.flush()
            return _batch_response(session, batch)

    @router.get("/batches/{batch_id}", response_model=BatchResponse)
    def get_batch(
        batch_id: str,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        return _batch_response(session, owned_batch(session, principal.user_id, batch_id))

    @router.get("/batches/{batch_id}/events", response_model=list[RunEventResponse])
    def get_batch_events(
        batch_id: str,
        request: Request,
        after: int = 0,
        limit: int = 200,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        owned_batch(session, principal.user_id, batch_id)
        last_event_id = request.headers.get("last-event-id")
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError as error:
                raise HTTPException(status_code=400, detail="Invalid Last-Event-ID.") from error
        if after < 0 or not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="Invalid event cursor or limit.")
        events = session.scalars(
            select(RunEvent)
            .where(
                RunEvent.batch_id == batch_id,
                RunEvent.owner_id == principal.user_id,
                RunEvent.sequence > after,
            )
            .order_by(RunEvent.sequence)
            .limit(limit)
        )
        return [
            RunEventResponse(
                id=event.id,
                sequence=event.sequence,
                event_type=event.event_type,
                phase=event.phase,
                status=event.status,
                progress=event.progress,
                item_id=event.item_id,
                detail=event.safe_detail,
                created_at=event.created_at,
            )
            for event in events
        ]

    @router.get("/batches/{batch_id}/events/stream")
    async def stream_batch_events(
        batch_id: str,
        request: Request,
        after: int = 0,
        principal: Principal = principal_dependency,
        session: Session = session_dependency,
    ):
        owned_batch(session, principal.user_id, batch_id)
        session.close()
        last_event_id = request.headers.get("last-event-id")
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError as error:
                raise HTTPException(status_code=400, detail="Invalid Last-Event-ID.") from error
        if after < 0:
            raise HTTPException(status_code=422, detail="Invalid event cursor.")

        async def event_stream():
            cursor = after
            while True:
                if await request.is_disconnected():
                    break
                with request.app.state.session_factory() as event_session:
                    rows = list(event_session.scalars(
                        select(RunEvent)
                        .where(
                            RunEvent.batch_id == batch_id,
                            RunEvent.owner_id == principal.user_id,
                            RunEvent.sequence > cursor,
                        )
                        .order_by(RunEvent.sequence)
                        .limit(100)
                    ))
                if rows:
                    for event in rows:
                        cursor = event.sequence
                        data = RunEventResponse(
                            id=event.id,
                            sequence=event.sequence,
                            event_type=event.event_type,
                            phase=event.phase,
                            status=event.status,
                            progress=event.progress,
                            item_id=event.item_id,
                            detail=event.safe_detail,
                            created_at=event.created_at,
                        ).model_dump_json()
                        yield f"id: {event.sequence}\nevent: progress\ndata: {data}\n\n"
                else:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(1)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/batches/{batch_id}/research-results", response_model=list[ResearchCard])
    def research_results(
        batch_id: str,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        owned_batch(session, principal.user_id, batch_id)
        items = session.scalars(
            select(BatchItem)
            .where(BatchItem.batch_id == batch_id, BatchItem.owner_id == principal.user_id)
            .order_by(BatchItem.created_at, BatchItem.id)
        )
        return [
            ResearchCard(
                item_id=item.id,
                status=item.status,
                decision=item.research_decision,
                mode=item.mode,
                company_name=(item.research_result or {}).get("company_name") or item.target_company,
                company_domain=(item.research_result or {}).get("company_domain") or item.target_domain,
                contact_name=(item.research_result or {}).get("contact_name"),
                contact_email=(item.research_result or {}).get("contact_email"),
                role=(item.research_result or {}).get("job_role") or item.target_role,
                job_description=(item.research_result or {}).get("job_description") or item.target_description,
                role_fit=(item.research_result or {}).get("role_fit"),
                evaluator_feedback=(item.research_result or {}).get("evaluator_feedback"),
                eligibility_reason=(item.research_result or {}).get("eligibility_reason") or (item.research_result or {}).get("evaluator_feedback"),
                summary=(item.research_result or {}).get("research_summary"),
                failure_code=item.failure_code,
                search_attempt=(item.research_result or {}).get("search_attempt", 0),
                search_budget=(item.research_result or {}).get("search_budget", 0),
                candidates_discovered=(item.research_result or {}).get("candidates_discovered"),
                candidates_evaluated=(item.research_result or {}).get("candidates_evaluated"),
                candidates_accepted=(item.research_result or {}).get("candidates_accepted"),
                rejected_candidates=(item.research_result or {}).get("rejected_candidates", []),
            )
            for item in items
        ]

    @router.post(
        "/batches/{batch_id}/items/{item_id}/research-decision",
        response_model=BatchItemResponse,
    )
    def decide_research(
        batch_id: str,
        item_id: str,
        payload: ResearchDecisionRequest,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        with session.begin():
            item = owned_item(session, principal.user_id, batch_id, item_id)
            decision = "approved" if payload.decision == "approve" else "rejected"
            if item.status != "research_review":
                if item.research_decision == decision:
                    return _item_response(item)
                raise HTTPException(status_code=409, detail="Research result is not awaiting a decision.")
            item.research_decision = decision
            item.updated_at = utc_now()
            run = session.scalar(
                select(ApplicationRun).where(
                    ApplicationRun.item_id == item_id,
                    ApplicationRun.owner_id == principal.user_id,
                )
            )
            if run is None:
                raise HTTPException(status_code=409, detail="Run record is unavailable.")
            if decision == "approved":
                item.status = "drafting"
                run.status = "queued"
                run.phase = "research_review"
                run.progress = 45
                _insert_outbox(
                    session, run, item, "resume_research",
                    f"research-decision:{run.id}:{run.attempt}:{decision}",
                    decision=decision,
                )
                append_event(
                    session, owner_id=principal.user_id, batch_id=batch_id, run_id=run.id,
                    event_type="research_approved", phase="research_review", status="queued",
                    progress=45, item_id=item_id, detail="Research approval saved; run resume queued.",
                )
            else:
                item.status = "queued" if item.mode == "speculative" else "rejected"
                run.status = "queued"
                run.phase = "research" if item.mode == "speculative" else "research_review"
                run.progress = 10 if item.mode == "speculative" else 45
                _insert_outbox(
                    session, run, item, "resume_research",
                    f"research-decision:{run.id}:{run.attempt}:{decision}",
                    decision=decision,
                )
                detail = (
                    "Candidate rejected by user; researching next candidate in speculative discovery."
                    if item.mode == "speculative"
                    else "Explicit user rejection: Research rejection saved; run resume queued."
                )
                append_event(
                    session, owner_id=principal.user_id, batch_id=batch_id, run_id=run.id,
                    event_type="research_rejected", phase=run.phase, status="queued",
                    progress=run.progress, item_id=item_id, detail=detail,
                )
            update_batch_status(session, batch_id, principal.user_id)
            session.flush()
            return _item_response(item)

    @router.post("/batches/{batch_id}/drafts/generate", response_model=BatchResponse, status_code=202)
    def generate_drafts(
        batch_id: str,
        request: Request,
        payload: DraftGenerationRequest | None = None,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        with session.begin():
            max_generations = request.app.state.settings.max_draft_generations
            batch = owned_batch(session, principal.user_id, batch_id)
            regenerate_ids = set(payload.regenerate_item_ids if payload else [])
            if regenerate_ids:
                for requested_id in regenerate_ids:
                    owned_item(session, principal.user_id, batch_id, requested_id)
            items = list(session.scalars(
                select(BatchItem).where(
                    BatchItem.batch_id == batch_id,
                    BatchItem.owner_id == principal.user_id,
                    BatchItem.research_decision == "approved",
                    *(
                        [BatchItem.id.in_(regenerate_ids)]
                        if regenerate_ids
                        else []
                    ),
                ).with_for_update()
            ))
            queued_count = 0
            if regenerate_ids and {item.id for item in items} != regenerate_ids:
                raise HTTPException(status_code=409, detail="Only research-approved items can be regenerated.")
            for item in items:
                if regenerate_ids and item.status in {"queued", "researching", "drafting"}:
                    raise HTTPException(status_code=409, detail="This item is already being processed.")
                current = list(session.scalars(
                    select(DraftVersion).where(
                        DraftVersion.item_id == item.id,
                        DraftVersion.owner_id == principal.user_id,
                    )
                ))
                run = session.scalar(select(ApplicationRun).where(
                    ApplicationRun.item_id == item.id,
                    ApplicationRun.owner_id == principal.user_id,
                ))
                if run is None:
                    continue
                if regenerate_ids:
                    if not current:
                        raise HTTPException(status_code=409, detail="This item has no drafts to regenerate.")
                    if run.attempt >= max_generations:
                        raise HTTPException(status_code=409, detail="Draft regeneration limit reached.")
                    run.attempt += 1
                elif current or item.status in {
                    "queued", "researching", "drafting", "draft_review", "completed",
                }:
                    continue
                item.status = "drafting"
                run.status = "queued"
                run.phase = "drafting"
                _insert_outbox(
                    session, run, item, "draft_item", f"draft:{run.id}:{run.attempt}"
                )
                append_event(
                    session, owner_id=principal.user_id, batch_id=batch_id, run_id=run.id,
                    event_type="draft_generation_queued", phase="drafting", status="queued",
                    progress=55, item_id=item.id, detail="Draft generation queued.",
                )
                queued_count += 1
            update_batch_status(session, batch.id, principal.user_id)
            session.flush()
            result = _batch_response(session, batch)
            if not queued_count:
                raise HTTPException(status_code=409, detail="No approved research items are ready for drafting.")
            return result

    @router.post(
            "/batches/{batch_id}/items/{item_id}/retry",
            response_model=BatchItemResponse,
    )
    def retry_failed_item(
            batch_id: str,
            item_id: str,
            request: Request,
            session: Session = session_dependency,
            principal: Principal = principal_dependency,
    ):
            with session.begin():
                item = owned_item(session, principal.user_id, batch_id, item_id)
                if item.status not in {"failed", "ineligible", "rejected"}:
                    raise HTTPException(status_code=409, detail="Only failed, ineligible, or rejected items can be retried.")

                settings = request.app.state.settings
                if settings.database_url.startswith("sqlite:") and (
                    item.failure_code == "worker_requires_postgresql"
                    or "require postgresql" in (item.failure_code or "").lower()
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Worker task requires PostgreSQL. Configure DATABASE_URL with a PostgreSQL database to retry.",
                    )

                run = session.scalar(
                    select(ApplicationRun)
                    .where(
                        ApplicationRun.item_id == item_id,
                        ApplicationRun.batch_id == batch_id,
                        ApplicationRun.owner_id == principal.user_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    raise HTTPException(status_code=409, detail="Run record is unavailable.")
                if run.attempt >= settings.max_draft_generations:
                    raise HTTPException(status_code=409, detail="Retry limit reached for this item.")

                # Prevent duplicate pending retry jobs
                pending_jobs = list(session.scalars(
                    select(OutboxMessage.id).where(
                        OutboxMessage.processed_at.is_(None),
                    )
                ))
                for p_id in pending_jobs:
                    msg = session.get(OutboxMessage, p_id)
                    if msg and (msg.payload or {}).get("run_id") == run.id:
                        raise HTTPException(status_code=409, detail="A retry job is already queued for this item.")

                if run.phase == "research" and item.research_decision is None:
                    topic = "research_item"
                    decision = None
                elif run.phase == "research_review" and item.research_decision in {"approved", "rejected"}:
                    topic = "resume_research"
                    decision = item.research_decision
                elif run.phase == "drafting" and item.research_decision == "approved":
                    topic = "draft_item"
                    decision = None
                else:
                    raise HTTPException(status_code=409, detail="This failed phase cannot be retried.")

                run.attempt += 1
                run.status = "queued"
                run.safe_error_code = None
                run.finished_at = None
                run.updated_at = utc_now()
                item.status = "queued"
                item.failure_code = None
                item.updated_at = utc_now()
                dedupe_key = (
                    f"retry:{topic}:{run.id}:{run.attempt}"
                )
                _insert_outbox(
                    session,
                    run,
                    item,
                    topic,
                    dedupe_key,
                    decision=decision,
                )
                append_event(
                    session,
                    owner_id=principal.user_id,
                    batch_id=batch_id,
                    run_id=run.id,
                    event_type="item_retry_queued",
                    phase=run.phase,
                    status="queued",
                    progress=run.progress,
                    item_id=item_id,
                    detail="Explicit item retry queued.",
                )
                update_batch_status(session, batch_id, principal.user_id)
                session.flush()
                return _item_response(item)

    @router.get(
        "/batches/{batch_id}/items/{item_id}/drafts",
        response_model=DraftResponse,
    )
    def get_drafts(
        batch_id: str,
        item_id: str,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        item = owned_item(session, principal.user_id, batch_id, item_id)
        docs: list[DraftDocument] = []
        for artifact_type in ("cover_letter", "email"):
            draft = latest_draft(session, principal.user_id, item_id, artifact_type)
            if draft is None:
                continue
            approval = session.scalar(
                select(ApprovalDecision).where(
                    ApprovalDecision.owner_id == principal.user_id,
                    ApprovalDecision.item_id == item_id,
                    ApprovalDecision.artifact_type == artifact_type,
                    ApprovalDecision.draft_version_id == draft.id,
                )
            )
            approval_state = approval.decision if approval else None
            delivery = None
            if artifact_type == "email":
                delivery = session.scalar(
                    select(DeliveryRecord).where(
                        DeliveryRecord.owner_id == principal.user_id,
                        DeliveryRecord.item_id == item_id,
                    )
                )
            delivery_version = None
            if delivery is not None:
                delivery_version = session.scalar(
                    select(DraftVersion.version).where(
                        DraftVersion.id == delivery.draft_version_id,
                        DraftVersion.owner_id == principal.user_id,
                    )
                )
            docs.append(DraftDocument(
                artifact_type=artifact_type,
                version=draft.version,
                content=draft.content,
                subject=draft.subject,
                approval=approval_state,
                delivery_status=delivery.status if delivery else None,
                delivery_version=delivery_version,
                delivery_id=delivery.id if delivery else None,
                delivery_queued_at=delivery.queued_at if delivery else None,
            ))
        attachments: list[str] = []
        clean_name = re.sub(r"[^\w\s-]", "", item.candidate_name or "Candidate").strip().replace(" ", "_")
        resume_ext = ".pdf"
        if item.cv_artifact_id:
            cv_art = session.get(PrivateArtifact, item.cv_artifact_id)
            if cv_art and cv_art.filename:
                suffix = Path(cv_art.filename).suffix
                if suffix:
                    resume_ext = suffix
        attachments.append(f"{clean_name}_Resume{resume_ext}")
        attachments.append(f"{clean_name}_Cover_Letter.pdf")
        return DraftResponse(item_id=item_id, documents=docs, attachments=attachments)

    @router.put(
        "/batches/{batch_id}/items/{item_id}/drafts/{version}",
        response_model=DraftDocument,
        status_code=status.HTTP_201_CREATED,
    )
    def edit_draft(
        batch_id: str,
        item_id: str,
        version: int,
        payload: DraftEditRequest,
        artifact_type: str,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        if artifact_type not in {"cover_letter", "email"}:
            raise HTTPException(status_code=422, detail="Unsupported draft artifact.")
        with session.begin():
            item = session.scalar(
                select(BatchItem).where(
                    BatchItem.id == item_id,
                    BatchItem.batch_id == batch_id,
                    BatchItem.owner_id == principal.user_id,
                ).with_for_update()
            )
            if item is None:
                raise HTTPException(status_code=404, detail="Batch item not found.")
            if item.status in {"queued", "drafting"}:
                raise HTTPException(status_code=409, detail="Draft generation is in progress.")
            current = latest_draft(session, principal.user_id, item_id, artifact_type)
            if current is None or current.version != version or payload.expected_current_version != version:
                raise HTTPException(status_code=409, detail="Draft changed; reload the latest version before editing.")
            next_version = version + 1
            new_draft = DraftVersion(
                id=str(uuid.uuid4()),
                owner_id=principal.user_id,
                batch_id=batch_id,
                item_id=item_id,
                artifact_type=artifact_type,
                version=next_version,
                subject=payload.subject if artifact_type == "email" else None,
                content=payload.content,
                created_by="user",
                created_at=utc_now(),
            )
            session.add(new_draft)
            item.status = "draft_review"
            item.updated_at = utc_now()
            run = session.scalar(select(ApplicationRun).where(
                ApplicationRun.item_id == item_id,
                ApplicationRun.owner_id == principal.user_id,
            ))
            if run:
                run.status = "paused"
                run.phase = "draft_review"
                run.progress = 90
                run.finished_at = None
                run.updated_at = utc_now()
                append_event(
                    session, owner_id=principal.user_id, batch_id=batch_id, run_id=run.id,
                    event_type="draft_edited", phase="draft_review", status="paused",
                    progress=90, item_id=item_id, detail="A new draft version was saved; prior approval no longer applies.",
                )
            session.flush()
            return DraftDocument(
                artifact_type=artifact_type,
                version=next_version,
                content=new_draft.content,
                subject=new_draft.subject,
                approval=None,
            )

    @router.post(
        "/batches/{batch_id}/items/{item_id}/approvals",
        response_model=ApprovalResponse,
    )
    def decide_draft(
        batch_id: str,
        item_id: str,
        payload: ApprovalRequest,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        with session.begin():
            item = owned_item(session, principal.user_id, batch_id, item_id)
            latest = latest_draft(session, principal.user_id, item_id, payload.artifact_type)
            if latest is None or latest.version != payload.draft_version:
                raise HTTPException(status_code=409, detail="Draft version is stale.")
            existing = session.scalar(select(ApprovalDecision).where(
                ApprovalDecision.item_id == item_id,
                ApprovalDecision.artifact_type == payload.artifact_type,
                ApprovalDecision.draft_version_id == latest.id,
                ApprovalDecision.owner_id == principal.user_id,
            ))
            if existing and existing.decision == payload.decision:
                return ApprovalResponse(
                    item_id=item_id,
                    artifact_type=payload.artifact_type,
                    draft_version=latest.version,
                    decision=existing.decision,
                    decided_at=existing.decided_at,
                )
            if item.status in {"queued", "drafting"}:
                raise HTTPException(status_code=409, detail="Draft generation is in progress.")
            if item.research_decision != "approved":
                raise HTTPException(status_code=409, detail="Research approval is required before draft approval.")
            if payload.decision == "approved":
                unresolved = find_unresolved_placeholders(latest.content)
                if latest.subject:
                    unresolved.extend(find_unresolved_placeholders(latest.subject))
                if unresolved:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Cannot approve draft containing unresolved placeholders: {', '.join(sorted(set(unresolved)))}. Please edit to resolve them.",
                    )
            if existing:
                existing.decision = payload.decision
                existing.decided_at = utc_now()
                decision = existing
            else:
                decision = ApprovalDecision(
                    id=str(uuid.uuid4()),
                    owner_id=principal.user_id,
                    batch_id=batch_id,
                    item_id=item_id,
                    artifact_type=payload.artifact_type,
                    draft_version_id=latest.id,
                    decision=payload.decision,
                    decided_by=principal.user_id,
                    decided_at=utc_now(),
                )
                session.add(decision)
            if payload.decision == "approved":
                email_latest = latest_draft(session, principal.user_id, item_id, "email")
                letter_latest = latest_draft(session, principal.user_id, item_id, "cover_letter")
                required = [draft for draft in (email_latest, letter_latest) if draft]
                approved = all(
                    session.scalar(select(ApprovalDecision.id).where(
                        ApprovalDecision.owner_id == principal.user_id,
                        ApprovalDecision.item_id == item_id,
                        ApprovalDecision.draft_version_id == draft.id,
                        ApprovalDecision.decision == "approved",
                    ))
                    for draft in required
                )
                item.status = "completed" if len(required) == 2 and approved else "draft_review"
                run = session.scalar(select(ApplicationRun).where(
                    ApplicationRun.item_id == item_id,
                    ApplicationRun.owner_id == principal.user_id,
                ))
                if run:
                    run.status = "completed" if item.status == "completed" else "paused"
                    run.phase = "draft_review"
                    run.progress = 100 if item.status == "completed" else 90
                    if item.status == "completed":
                        run.finished_at = utc_now()
                    detail = f"{payload.artifact_type.replace('_', ' ').title()} approved."
                    append_event(
                        session, owner_id=principal.user_id, batch_id=batch_id, run_id=run.id,
                        event_type="draft_approved",
                        phase="draft_review", status=run.status, progress=run.progress,
                        item_id=item_id, detail=detail,
                    )
            else:
                run = session.scalar(select(ApplicationRun).where(
                    ApplicationRun.item_id == item_id,
                    ApplicationRun.owner_id == principal.user_id,
                ))
                if run is None:
                    raise HTTPException(status_code=409, detail="Run record is unavailable.")

                if payload.artifact_type == "cover_letter":
                    email_latest = latest_draft(session, principal.user_id, item_id, "email")
                    if email_latest:
                        prior_email_approval = session.scalar(select(ApprovalDecision).where(
                            ApprovalDecision.item_id == item_id,
                            ApprovalDecision.artifact_type == "email",
                            ApprovalDecision.draft_version_id == email_latest.id,
                            ApprovalDecision.owner_id == principal.user_id,
                        ))
                        if prior_email_approval:
                            session.delete(prior_email_approval)

                run.attempt += 1
                item.status = "drafting"
                run.status = "queued"
                run.phase = "drafting"
                run.progress = 55
                run.finished_at = None
                run.updated_at = utc_now()
                item.updated_at = utc_now()

                dedupe_key = f"draft:{run.id}:{run.attempt}"
                _insert_outbox(
                    session,
                    run,
                    item,
                    "draft_item",
                    dedupe_key,
                    extra={"rejected_artifact": payload.artifact_type},
                )
                detail = f"{payload.artifact_type.replace('_', ' ').title()} rejected; draft regeneration queued."
                append_event(
                    session, owner_id=principal.user_id, batch_id=batch_id, run_id=run.id,
                    event_type="draft_rejected",
                    phase="draft_review", status="queued", progress=55,
                    item_id=item_id, detail=detail,
                )
                append_event(
                    session, owner_id=principal.user_id, batch_id=batch_id, run_id=run.id,
                    event_type="draft_generation_queued",
                    phase="drafting", status="queued", progress=55,
                    item_id=item_id, detail=f"Draft generation queued for {payload.artifact_type.replace('_', ' ')} regeneration.",
                )
            update_batch_status(session, batch_id, principal.user_id)
            session.flush()
            return ApprovalResponse(
                item_id=item_id,
                artifact_type=payload.artifact_type,
                draft_version=latest.version,
                decision=payload.decision,
                decided_at=decision.decided_at,
            )

    @router.post(
        "/batches/{batch_id}/items/{item_id}/approvals/combined",
        response_model=list[ApprovalResponse],
    )
    def approve_both_drafts(
        batch_id: str,
        item_id: str,
        payload: CombinedApprovalRequest,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        with session.begin():
            item = owned_item(session, principal.user_id, batch_id, item_id)
            if item.status in {"queued", "drafting"}:
                raise HTTPException(status_code=409, detail="Draft generation is in progress.")
            if item.research_decision != "approved":
                raise HTTPException(status_code=409, detail="Research approval is required before draft approval.")

            approvals = []
            for artifact_type, expected_version in (
                ("cover_letter", payload.cover_letter_version),
                ("email", payload.email_version),
            ):
                latest = latest_draft(session, principal.user_id, item_id, artifact_type)
                if latest is None or latest.version != expected_version:
                    raise HTTPException(status_code=409, detail="Draft version is stale.")
                unresolved = find_unresolved_placeholders(latest.content)
                if latest.subject:
                    unresolved.extend(find_unresolved_placeholders(latest.subject))
                if unresolved:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Cannot approve {artifact_type.replace('_', ' ')} containing unresolved placeholders: {', '.join(sorted(set(unresolved)))}. Please edit to resolve them.",
                    )
                decision = session.scalar(select(ApprovalDecision).where(
                    ApprovalDecision.item_id == item_id,
                    ApprovalDecision.artifact_type == artifact_type,
                    ApprovalDecision.draft_version_id == latest.id,
                    ApprovalDecision.owner_id == principal.user_id,
                ))
                if decision and decision.decision != "approved":
                    raise HTTPException(
                        status_code=409,
                        detail="A decision already exists for an exact draft version.",
                    )
                if decision is None:
                    decision = ApprovalDecision(
                        id=str(uuid.uuid4()),
                        owner_id=principal.user_id,
                        batch_id=batch_id,
                        item_id=item_id,
                        artifact_type=artifact_type,
                        draft_version_id=latest.id,
                        decision="approved",
                        decided_by=principal.user_id,
                        decided_at=utc_now(),
                    )
                    session.add(decision)
                approvals.append((latest, decision))

            item.status = "completed"
            item.updated_at = utc_now()
            run = session.scalar(select(ApplicationRun).where(
                ApplicationRun.item_id == item_id,
                ApplicationRun.owner_id == principal.user_id,
            ))
            if run:
                run.status = "completed"
                run.phase = "draft_review"
                run.progress = 100
                run.finished_at = utc_now()
                run.updated_at = utc_now()
                append_event(
                    session,
                    owner_id=principal.user_id,
                    batch_id=batch_id,
                    run_id=run.id,
                    event_type="drafts_approved",
                    phase="draft_review",
                    status="completed",
                    progress=100,
                    item_id=item_id,
                    detail="Both current draft versions were approved.",
                )
            update_batch_status(session, batch_id, principal.user_id)
            session.flush()
            return [
                ApprovalResponse(
                    item_id=item_id,
                    artifact_type=draft.artifact_type,
                    draft_version=draft.version,
                    decision=decision.decision,
                    decided_at=decision.decided_at,
                )
                for draft, decision in approvals
            ]

    @router.get("/runs/{run_id}", response_model=RunStatusResponse)
    def get_run(
        run_id: str,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        run = session.scalar(select(ApplicationRun).where(
            ApplicationRun.id == run_id,
            ApplicationRun.owner_id == principal.user_id,
        ))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return RunStatusResponse(
            id=run.id,
            batch_id=run.batch_id,
            item_id=run.item_id,
            status=run.status,
            phase=run.phase,
            progress=run.progress,
            updated_at=run.updated_at,
        )

    return router


def _insert_outbox(
    session: Session,
    run: ApplicationRun,
    item: BatchItem,
    topic: str,
    dedupe_key: str,
    *,
    decision: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    exists = session.scalar(select(OutboxMessage.id).where(OutboxMessage.dedupe_key == dedupe_key))
    if exists:
        return
    payload = {
        "run_id": run.id,
        "batch_id": run.batch_id,
        "item_id": item.id,
        "owner_id": run.owner_id,
        "thread_id": run.thread_id,
    }
    if decision:
        payload["decision"] = decision
    if extra:
        payload.update(extra)
    session.add(OutboxMessage(
        id=str(uuid.uuid4()),
        topic=topic,
        dedupe_key=dedupe_key,
        payload=payload,
        available_at=utc_now(),
        attempts=0,
        created_at=utc_now(),
    ))


def _item_response(item: BatchItem) -> BatchItemResponse:
    result = item.research_result or {}
    return BatchItemResponse(
        id=item.id,
        status=item.status,
        mode=item.mode,
        company_name=result.get("company_name") or item.target_company,
        domain=result.get("company_domain") or item.target_domain,
        role=result.get("job_role") or item.target_role,
        research_decision=item.research_decision,
        failure_code=item.failure_code,
    )


def _batch_response(session: Session, batch: OutreachBatch) -> BatchResponse:
    items = list(session.scalars(
        select(BatchItem).where(
            BatchItem.batch_id == batch.id,
            BatchItem.owner_id == batch.owner_id,
        ).order_by(BatchItem.created_at, BatchItem.id)
    ))
    completed = sum(item.status in {"completed", "rejected", "ineligible"} for item in items)
    failed = sum(item.status == "failed" for item in items)
    batch_mode = items[0].mode if items else "open_role"
    return BatchResponse(
        id=batch.id,
        label=batch.label,
        status=batch.status,
        mode=batch_mode,
        is_archived=bool(getattr(batch, "is_archived", False)),
        created_at=batch.created_at,
        item_count=len(items),
        completed_count=completed,
        failed_count=failed,
        items=[_item_response(item) for item in items],
    )
