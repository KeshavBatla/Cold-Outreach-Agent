from __future__ import annotations
import sys
import hashlib
import logging
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode
import config
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.errors import HttpError
from oauthlib.oauth2 import OAuth2Error
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from persistence import (
    ApprovalDecision,
    BatchItem,
    DeliveryRecord,
    DraftVersion,
    GmailConnection,
    PrivateArtifact,
    utc_now,
)
from tools.pdf_generator import render_cover_letter_pdf
from utils.helpers import find_unresolved_placeholders
from webapp.auth import Principal, get_current_principal
from webapp.database import get_session
from webapp.gmail_delivery import (
    GmailConfigurationError,
    GmailTokenError,
    build_gmail_service,
    create_oauth_flow,
    credentials_from_token,
    decrypt_token,
    encrypt_token,
    execute_gmail_send,
    missing_oauth_settings,
    make_oauth_state_cookie,
    refresh_credentials,
    validate_token_encryption_config,
    verify_oauth_state_cookie,
)
from webapp.schemas import (
    DeliveryResponse,
    GmailConnectionResponse,
    GmailDeliveryRecoveryRequest,
    GmailOAuthStartResponse,
    GmailSendRequest,
)
from webapp.settings import Settings

_OAUTH_STATE_COOKIE = "gmail_oauth_state"
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
logger = logging.getLogger(__name__)

_SAFE_OAUTH_CATEGORIES = {
    "access_denied",
    "invalid_client",
    "invalid_grant",
    "invalid_request",
    "invalid_scope",
    "redirect_uri_mismatch",
    "unauthorized_client",
}
_SAFE_MESSAGE_PATTERNS = (
    (re.compile(r"invalid[_ ]grant", re.IGNORECASE), "invalid_grant"),
    (re.compile(r"invalid[_ ]client", re.IGNORECASE), "invalid_client"),
    (re.compile(r"redirect[_ ]uri[_ ]mismatch", re.IGNORECASE), "redirect_uri_mismatch"),
    (re.compile(r"timed?\s*out|timeout", re.IGNORECASE), "request_timeout"),
    (re.compile(r"connection\s+(?:refused|reset|aborted)", re.IGNORECASE), "connection_error"),
    (re.compile(r"database|sqlalchemy|postgres|sqlite", re.IGNORECASE), "database_error"),
    (re.compile(r"encrypt|cipher|key", re.IGNORECASE), "credential_encryption_error"),
    (re.compile(r"scope|permission", re.IGNORECASE), "scope_validation_error"),
    (re.compile(r"refresh", re.IGNORECASE), "refresh_token_error"),
)


def _google_oauth_error_details(
    error: Exception,
) -> tuple[str, int | None, str | None] | None:
    if not isinstance(error, (GoogleAuthError, OAuth2Error, HttpError)):
        return None

    status_code = getattr(error, "status_code", None)

    if status_code is None:
        status_code = getattr(getattr(error, "resp", None), "status", None)

    if not isinstance(status_code, int):
        status_code = None

    raw_category = getattr(error, "error", None)

    if not isinstance(raw_category, str):
        raw_category = getattr(error, "reason", None)

    category = (
        raw_category.casefold()
        if isinstance(raw_category, str)
        and raw_category.casefold() in _SAFE_OAUTH_CATEGORIES
        else "google_oauth_error"
    )

    # OAuth libraries may expose Google's error description here.
    # Only allow safe, short descriptions; never log tokens or codes.
    description = getattr(error, "description", None)

    if not isinstance(description, str):
        description = getattr(error, "error_description", None)

    safe_description = None

    if isinstance(description, str):
        normalized = " ".join(description.split())

        if (
            len(normalized) <= 240
            and re.fullmatch(r"[A-Za-z0-9 .,;_()'/-]+", normalized)
        ):
            safe_description = normalized

    return category, status_code, safe_description


def _safe_exception_message(error: Exception) -> str:
    message = str(error)
    safe_category = next(
        (category for pattern, category in _SAFE_MESSAGE_PATTERNS if pattern.search(message)),
        None,
    )
    if safe_category:
        return safe_category
    return "details omitted"


def build_gmail_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/gmail", tags=["gmail"])
    session_dependency = Depends(get_session)
    principal_dependency = Depends(get_current_principal)

    @router.get("/connection", response_model=GmailConnectionResponse)
    def connection_status(
        request: Request,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        connection = session.scalar(
            select(GmailConnection).where(GmailConnection.owner_id == principal.user_id)
        )
        connected = bool(
            connection
            and connection.status == "connected"
            and connection.token_secret_ref
        )
        connection_status = connection.status if connection else "unlinked"
        if connected:
            try:
                token_json = decrypt_token(
                    connection.owner_id,
                    connection.id,
                    connection.token_secret_ref,
                )
                credentials_from_token(token_json)
            except (GmailConfigurationError, GmailTokenError):
                connected = False
                connection_status = "invalid"
        settings: Settings = request.app.state.settings
        return GmailConnectionResponse(
            connected=connected,
            status=connection_status,
            test_mode=settings.gmail_test_mode,
            live_enabled=(
                settings.gmail_live_send_enabled
                and not settings.gmail_test_mode
                and settings.app_environment != "test"
            ),
            account_email=connection.account_email if connected else None,
        )

    @router.post("/oauth/start", response_model=GmailOAuthStartResponse)
    def start_oauth(
        request: Request,
        response: Response,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        del session, principal
        settings: Settings = request.app.state.settings
        missing = missing_oauth_settings(settings)
        if missing:
            raise HTTPException(
                status_code=503,
                detail="Gmail OAuth is not configured. Set " + ", ".join(missing) + ".",
            )
        try:
            validate_token_encryption_config()
        except GmailConfigurationError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        frontend_origin = request.headers.get("origin", "").rstrip("/")
        if frontend_origin not in settings.allowed_origins:
            raise HTTPException(status_code=403, detail="Request origin is not allowed.")
        state = secrets.token_urlsafe(32)
        try:
            authorization_url, _ = create_oauth_flow(settings, state).authorization_url(
                access_type="offline",
                prompt="consent",
            )
        except GmailConfigurationError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        response.set_cookie(
            _OAUTH_STATE_COOKIE,
            make_oauth_state_cookie(state, frontend_origin),
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite="lax",
            path="/api/v1/gmail",
            max_age=600,
        )
        return GmailOAuthStartResponse(authorization_url=authorization_url)

    @router.get("/oauth/callback")
    def oauth_callback(
        request: Request,
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        settings: Settings = request.app.state.settings
        cookie_value = request.cookies.get(_OAUTH_STATE_COOKIE, "")
        frontend_origin = verify_oauth_state_cookie(cookie_value, state or "")
        if not state or not frontend_origin or frontend_origin not in settings.allowed_origins:
            logger.warning(
                "gmail_oauth_callback_failed stage=callback_state_validation category=state_validation_failed"
            )
            raise HTTPException(status_code=400, detail="Gmail OAuth state validation failed.")

        def redirect_gmail_result(result: str) -> RedirectResponse:
            redirect = RedirectResponse(
                f"{frontend_origin}/?{urlencode({'gmail': result})}",
                status_code=303,
            )
            redirect.delete_cookie(
                _OAUTH_STATE_COOKIE,
                path="/api/v1/gmail",
                secure=settings.session_cookie_secure,
                httponly=True,
                samesite="lax",
            )
            return redirect

        if error or not code:
            safe_category = (
                error.casefold()
                if error and error.casefold() in _SAFE_OAUTH_CATEGORIES
                else "authorization_cancelled"
            )
            logger.info(
                "gmail_oauth_callback_cancelled stage=authorization_response category=%s",
                safe_category,
            )
            return redirect_gmail_result("authorization-cancelled")

        stage = "authorization_code_exchange"
        try:
            flow = create_oauth_flow(settings, state)
            flow.fetch_token(code=code)
            credentials = flow.credentials

            stage = "granted_scope_validation"
            scopes = sorted(set(credentials.scopes or []))
            if set(scopes) - set(config.GMAIL_SCOPES):
                raise GmailTokenError("Google returned an unexpected Gmail permission.")
            if not set(config.GMAIL_SCOPES).issubset(scopes):
                raise GmailTokenError("Google authorization did not grant the required Gmail send scope.")

            stage = "refresh_token_validation"
            if not credentials.refresh_token:
                raise GmailTokenError("Google authorization did not provide offline refresh access.")

            stage = "database_persistence"
            existing = session.scalar(
                select(GmailConnection).where(GmailConnection.owner_id == principal.user_id)
            )
            connection = existing or GmailConnection(
                id=str(uuid.uuid4()),
                owner_id=principal.user_id,
                status="unlinked",
                scopes=[],
                updated_at=utc_now(),
            )
            stage = "credential_encryption"
            token_json = credentials.to_json()
            encrypted_token = encrypt_token(
                principal.user_id,
                connection.id,
                token_json,
            )

            stage = "database_persistence"
            connection.status = "connected"
            connection.scopes = scopes
            connection.token_secret_ref = encrypted_token
            connection.linked_at = utc_now()
            connection.updated_at = utc_now()
            if existing is None:
                session.add(connection)
            session.commit()
        except Exception as exception:
            session.rollback()

            google_error = _google_oauth_error_details(exception)

            print(
                "\n========== GMAIL OAUTH DIAGNOSTIC ==========",
                file=sys.stderr,
                flush=True,
            )
            print(f"Failure stage: {stage}", file=sys.stderr, flush=True)
            print(
                f"Exception type: {type(exception).__name__}",
                file=sys.stderr,
                flush=True,
            )

            if google_error is not None:
                category, http_status, description = google_error

                logger.warning(
                    "gmail_oauth_callback_failed stage=%s oauth_error_category=%s http_status=%s",
                    stage,
                    category,
                    http_status,
                )

                print(
                    f"Google OAuth error category: {category}",
                    file=sys.stderr,
                    flush=True,
                )

                print(
                    f"HTTP status: {http_status}",
                    file=sys.stderr,
                    flush=True,
                )

                if description:
                    print(
                        f"Google error description: {description}",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                safe_msg = _safe_exception_message(exception)
                logger.warning(
                    "gmail_oauth_callback_failed stage=%s exception_class=%s message=%s",
                    stage,
                    type(exception).__name__,
                    safe_msg,
                )
                print(
                    f"Safe error message: {safe_msg}",
                    file=sys.stderr,
                    flush=True,
                )

            return redirect_gmail_result("authorization-failed")
        return redirect_gmail_result("connected")

    @router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
    def disconnect(
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        connection = session.scalar(
            select(GmailConnection).where(
                GmailConnection.owner_id == principal.user_id,
            ).with_for_update()
        )
        if connection is None or connection.status != "connected" or not connection.token_secret_ref:
            return Response(status_code=204)
        try:
            token_json = decrypt_token(
                principal.user_id,
                connection.id,
                connection.token_secret_ref,
            )
            credentials = credentials_from_token(token_json)
            credentials.revoke(GoogleRequest())
        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail="Google could not confirm token revocation. The connection remains linked.",
            ) from error
        connection.status = "revoked"
        connection.token_secret_ref = None
        connection.updated_at = utc_now()
        session.commit()
        return Response(status_code=204)

    @router.post(
        "/batches/{batch_id}/items/{item_id}/send",
        response_model=DeliveryResponse,
    )
    def send_email(
        batch_id: str,
        item_id: str,
        payload: GmailSendRequest,
        request: Request,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        settings: Settings = request.app.state.settings
        if not settings.gmail_test_mode and (
            not settings.gmail_live_send_enabled or settings.app_environment == "test"
        ):
            raise HTTPException(
                status_code=503,
                detail="Live Gmail delivery is disabled. Configure GMAIL_TEST_MODE=false and GMAIL_LIVE_SEND_ENABLED=true.",
            )

        idempotency_key = hashlib.sha256(
            f"gmail-delivery-v1:{principal.user_id}:{item_id}".encode("utf-8")
        ).hexdigest()
        try:
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

                existing_delivery = session.scalar(
                    select(DeliveryRecord).where(
                        DeliveryRecord.owner_id == principal.user_id,
                        DeliveryRecord.item_id == item_id,
                    ).with_for_update()
                )
                if existing_delivery is not None:
                    return _delivery_response(session, existing_delivery)

                research_result = item.research_result or {}
                if (
                    item.research_decision != "approved"
                    or item.status not in {"draft_review", "completed"}
                    or item.status in {"ineligible", "rejected", "failed"}
                    or item.failure_code is not None
                    or research_result.get("role_fit") != "suitable"
                ):
                    raise HTTPException(status_code=409, detail="Approved, eligible research is required before sending.")
                draft = session.scalar(
                    select(DraftVersion).where(
                        DraftVersion.owner_id == principal.user_id,
                        DraftVersion.batch_id == batch_id,
                        DraftVersion.item_id == item_id,
                        DraftVersion.artifact_type == "email",
                        DraftVersion.version == payload.draft_version,
                    )
                )
                latest = session.scalar(
                    select(DraftVersion).where(
                        DraftVersion.owner_id == principal.user_id,
                        DraftVersion.item_id == item_id,
                        DraftVersion.artifact_type == "email",
                    ).order_by(DraftVersion.version.desc()).limit(1)
                )
                if draft is None or latest is None or draft.id != latest.id:
                    raise HTTPException(status_code=409, detail="Email draft version is stale.")
                approval = session.scalar(
                    select(ApprovalDecision).where(
                        ApprovalDecision.owner_id == principal.user_id,
                        ApprovalDecision.item_id == item_id,
                        ApprovalDecision.artifact_type == "email",
                        ApprovalDecision.draft_version_id == draft.id,
                        ApprovalDecision.decision == "approved",
                    )
                )
                if approval is None:
                    raise HTTPException(status_code=409, detail="The current email draft must be approved before sending.")
                recipient = research_result.get("contact_email")
                if not isinstance(recipient, str) or not _EMAIL_PATTERN.fullmatch(recipient.strip()):
                    raise HTTPException(status_code=409, detail="A valid recipient email address is required.")
                recipient = recipient.strip()
                subject = (draft.subject or "").strip()
                body = draft.content.strip()
                if not subject or not body:
                    raise HTTPException(status_code=409, detail="The saved email must include a subject and body.")

                unresolved_email = find_unresolved_placeholders(body) + find_unresolved_placeholders(subject)
                if unresolved_email:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Cannot send email containing unresolved placeholders: {', '.join(sorted(set(unresolved_email)))}.",
                    )

                connection = session.scalar(
                    select(GmailConnection).where(
                        GmailConnection.owner_id == principal.user_id,
                        GmailConnection.status == "connected",
                    )
                )
                if connection is None or not connection.token_secret_ref:
                    raise HTTPException(status_code=409, detail="Connect a Gmail account before sending.")

                if not item.cv_artifact_id:
                    raise HTTPException(status_code=409, detail="A candidate resume artifact is required before sending.")
                cv_artifact = session.scalar(
                    select(PrivateArtifact).where(
                        PrivateArtifact.id == item.cv_artifact_id,
                        PrivateArtifact.owner_id == principal.user_id,
                    )
                )
                if cv_artifact is None:
                    raise HTTPException(status_code=409, detail="Candidate resume artifact was not found.")
                resume_path = (settings.private_artifact_dir / cv_artifact.storage_key).resolve()
                if not resume_path.is_relative_to(settings.private_artifact_dir) or not resume_path.is_file():
                    raise HTTPException(status_code=409, detail="Candidate resume file could not be located in storage.")
                resume_bytes = resume_path.read_bytes()
                if not resume_bytes:
                    raise HTTPException(status_code=409, detail="Candidate resume file is empty or corrupted.")

                cover_letter_draft = session.scalar(
                    select(DraftVersion).where(
                        DraftVersion.owner_id == principal.user_id,
                        DraftVersion.item_id == item_id,
                        DraftVersion.artifact_type == "cover_letter",
                    ).order_by(DraftVersion.version.desc()).limit(1)
                )
                cl_pdf_bytes = None
                if cover_letter_draft is not None:
                    cl_approval = session.scalar(
                        select(ApprovalDecision).where(
                            ApprovalDecision.owner_id == principal.user_id,
                            ApprovalDecision.item_id == item_id,
                            ApprovalDecision.artifact_type == "cover_letter",
                            ApprovalDecision.draft_version_id == cover_letter_draft.id,
                            ApprovalDecision.decision == "approved",
                        )
                    )
                    if cl_approval is None:
                        raise HTTPException(status_code=409, detail="The cover letter must be approved before sending.")
                    unresolved_cl = find_unresolved_placeholders(cover_letter_draft.content)
                    if unresolved_cl:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Cannot send cover letter containing unresolved placeholders: {', '.join(sorted(set(unresolved_cl)))}.",
                        )
                    clean_cand_name = (item.candidate_name or "Candidate").strip()
                    try:
                        cl_pdf_bytes = render_cover_letter_pdf(
                            cover_letter_draft.content,
                            title=f"Cover Letter — {clean_cand_name}",
                        )
                    except Exception as exc:
                        raise HTTPException(status_code=500, detail="Failed to render cover letter PDF.") from exc

                clean_name = re.sub(r"[^\w\s-]", "", item.candidate_name or "Candidate").strip().replace(" ", "_")
                resume_filename = f"{clean_name}_Resume{resume_path.suffix}"
                resume_mimetype = cv_artifact.media_type or (
                    "application/pdf" if resume_path.suffix.lower() == ".pdf" else "application/octet-stream"
                )
                send_attachments = [
                    {
                        "filename": resume_filename,
                        "content": resume_bytes,
                        "mimetype": resume_mimetype,
                    }
                ]
                if cl_pdf_bytes:
                    cl_filename = f"{clean_name}_Cover_Letter.pdf"
                    send_attachments.append({
                        "filename": cl_filename,
                        "content": cl_pdf_bytes,
                        "mimetype": "application/pdf",
                    })

                delivery = DeliveryRecord(
                    id=str(uuid.uuid4()),
                    owner_id=principal.user_id,
                    batch_id=batch_id,
                    item_id=item_id,
                    artifact_type="email",
                    draft_version_id=draft.id,
                    approval_decision="approved",
                    idempotency_key=idempotency_key,
                    status="queued",
                    queued_at=utc_now(),
                )
                session.add(delivery)
                session.flush()
                delivery_id = delivery.id
                connection_id = connection.id
                encrypted_token = connection.token_secret_ref
                token_owner_id = connection.owner_id
        except IntegrityError:
            session.rollback()
            existing_delivery = session.scalar(
                select(DeliveryRecord).where(
                    DeliveryRecord.idempotency_key == idempotency_key,
                )
            )
            if existing_delivery is None:
                raise
            return _delivery_response(session, existing_delivery)

        with session.begin():
            claimed = session.execute(
                update(DeliveryRecord)
                .where(
                    DeliveryRecord.id == delivery_id,
                    DeliveryRecord.status == "queued",
                )
                .values(status="sending")
            )
            if claimed.rowcount != 1:
                existing_delivery = session.get(DeliveryRecord, delivery_id)
                if existing_delivery is None:
                    raise HTTPException(status_code=500, detail="Delivery reservation was lost.")
                return _delivery_response(session, existing_delivery)

        if settings.gmail_test_mode:
            return _finish_delivery(
                session,
                delivery_id,
                status_value="simulated",
                safe_error_code=None,
            )

        try:
            token_json = decrypt_token(token_owner_id, connection_id, encrypted_token)
            credentials = credentials_from_token(token_json)
            refresh_credentials(credentials)
        except Exception:
            return _finish_delivery(
                session,
                delivery_id,
                status_value="failed",
                safe_error_code="gmail_credentials_unavailable",
            )

        if credentials.token:
            connection_unavailable = False
            try:
                with session.begin():
                    stored_connection = session.get(GmailConnection, connection_id)
                    if stored_connection is None or stored_connection.status != "connected":
                        connection_unavailable = True
                    else:
                        stored_connection.token_secret_ref = encrypt_token(
                            token_owner_id,
                            connection_id,
                            credentials.to_json(),
                        )
                        stored_connection.updated_at = utc_now()
            except Exception:
                return _finish_delivery(
                    session,
                    delivery_id,
                    status_value="failed",
                    safe_error_code="gmail_credentials_persistence_failed",
                )
            if connection_unavailable:
                return _finish_delivery(
                    session,
                    delivery_id,
                    status_value="failed",
                    safe_error_code="gmail_connection_unavailable",
                )

        try:
            service = build_gmail_service(credentials)
        except Exception:
            return _finish_delivery(
                session,
                delivery_id,
                status_value="failed",
                safe_error_code="gmail_client_unavailable",
            )

        try:
            message_id = execute_gmail_send(service, recipient, subject, body, attachments=send_attachments)
            if not message_id:
                return _finish_delivery(
                    session,
                    delivery_id,
                    status_value="unknown",
                    safe_error_code="gmail_missing_message_id",
                )
            return _finish_delivery(
                session,
                delivery_id,
                status_value="sent",
                safe_error_code=None,
                provider_message_id=message_id,
            )
        except HttpError as error:
            provider_status = getattr(error, "status_code", None)
            if provider_status is None:
                provider_status = getattr(getattr(error, "resp", None), "status", None)
            if isinstance(provider_status, int) and 400 <= provider_status < 500:
                return _finish_delivery(
                    session,
                    delivery_id,
                    status_value="failed",
                    safe_error_code=f"gmail_http_{provider_status}",
                )
            return _finish_delivery(
                session,
                delivery_id,
                status_value="unknown",
                safe_error_code="gmail_response_ambiguous",
            )
        except Exception:
            return _finish_delivery(
                session,
                delivery_id,
                status_value="unknown",
                safe_error_code="gmail_response_ambiguous",
            )

    @router.post(
        "/deliveries/{delivery_id}/resolve-unknown",
        response_model=DeliveryResponse,
    )
    def resolve_interrupted_delivery(
        delivery_id: str,
        payload: GmailDeliveryRecoveryRequest,
        session: Session = session_dependency,
        principal: Principal = principal_dependency,
    ):
        if not payload.checked_sent_folder:
            raise HTTPException(
                status_code=409,
                detail="Check the connected Gmail account's Sent folder before resolving an interrupted delivery.",
            )
        with session.begin():
            delivery = session.scalar(
                select(DeliveryRecord).where(
                    DeliveryRecord.id == delivery_id,
                    DeliveryRecord.owner_id == principal.user_id,
                ).with_for_update()
            )
            if delivery is None:
                raise HTTPException(status_code=404, detail="Delivery record not found.")
            if delivery.status not in {"queued", "sending"}:
                raise HTTPException(status_code=409, detail="Only an interrupted delivery can be marked unknown.")
            if delivery.queued_at > utc_now() - timedelta(minutes=5):
                raise HTTPException(status_code=409, detail="Wait five minutes before resolving an in-progress send.")
            delivery.status = "unknown"
            delivery.safe_error_code = "manual_recovery_unknown"
            delivery.completed_at = utc_now()
            session.flush()
            return _delivery_response(session, delivery)

    return router


def _delivery_response(session: Session, record: DeliveryRecord) -> DeliveryResponse:
    return DeliveryResponse(
        id=record.id,
        status=record.status,
        draft_version=_version_from_record(session, record),
        provider_message_id=record.provider_message_id,
        safe_error_code=record.safe_error_code,
        queued_at=record.queued_at,
        completed_at=record.completed_at,
    )


def _finish_delivery(
    session: Session,
    delivery_id: str,
    *,
    status_value: str,
    safe_error_code: str | None,
    provider_message_id: str | None = None,
) -> DeliveryResponse:
    with session.begin():
        delivery = session.scalar(
            select(DeliveryRecord).where(
                DeliveryRecord.id == delivery_id,
            ).with_for_update()
        )
        if delivery is None:
            raise HTTPException(status_code=500, detail="Delivery record is unavailable.")
        delivery.status = status_value
        delivery.safe_error_code = safe_error_code
        delivery.provider_message_id = provider_message_id
        delivery.completed_at = utc_now()
        session.flush()
        return DeliveryResponse(
            id=delivery.id,
            status=delivery.status,
            draft_version=_version_from_record(session, delivery),
            provider_message_id=delivery.provider_message_id,
            safe_error_code=delivery.safe_error_code,
            queued_at=delivery.queued_at,
            completed_at=delivery.completed_at,
        )


def _version_from_record(session: Session, delivery: DeliveryRecord) -> int:
    version = session.scalar(
        select(DraftVersion.version).where(
            DraftVersion.id == delivery.draft_version_id,
            DraftVersion.owner_id == delivery.owner_id,
        )
    )
    return version or 1
