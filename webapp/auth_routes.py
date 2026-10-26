from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence import AppUser, LocalCredential, WebAuthSession, utc_now
from webapp.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL,
    _DUMMY_PASSWORD_HASH,
    Principal,
    _allowed_origin,
    _token_hash,
    get_current_principal,
    principal_from_session,
    verify_password,
)
from webapp.database import get_session
from webapp.schemas import AuthSessionResponse, AuthUserResponse, LoginRequest
from webapp.settings import Settings


def build_auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])

    @router.get("/session", response_model=AuthSessionResponse)
    def current_session(request: Request, session: Session = Depends(get_session)):
        principal = principal_from_session(request, session)
        if principal is None:
            response = AuthSessionResponse(authenticated=False)
            return response
        return AuthSessionResponse(
            authenticated=True,
            user=AuthUserResponse(login=principal.subject, email=principal.email),
        )

    @router.post("/login", response_model=AuthSessionResponse)
    def login(
        request: Request,
        payload: LoginRequest,
        response: Response,
        session: Session = Depends(get_session),
    ):
        settings: Settings = request.app.state.settings
        if not _allowed_origin(request, settings):
            raise HTTPException(status_code=403, detail="Request origin is not allowed.")

        login_name = payload.login.strip().casefold()
        credential = session.scalar(
            select(LocalCredential).where(LocalCredential.login_name == login_name)
        )
        stored_hash = credential.password_hash if credential else None
        if not verify_password(payload.password, stored_hash or _DUMMY_PASSWORD_HASH):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid login or password.",
            )
        assert credential is not None
        user = session.get(AppUser, credential.user_id)
        if (
            user is None
            or user.identity_provider != "local"
            or user.external_subject != credential.login_name
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid login or password.",
            )

        raw_session = secrets.token_urlsafe(32)
        raw_csrf = secrets.token_urlsafe(32)
        expires_at = utc_now() + SESSION_TTL
        session.add(
            WebAuthSession(
                token_hash=_token_hash(raw_session),
                user_id=user.id,
                csrf_token_hash=_token_hash(raw_csrf),
                expires_at=expires_at,
                created_at=utc_now(),
            )
        )
        session.commit()
        cookie_options = {
            "secure": settings.session_cookie_secure,
            "httponly": True,
            "samesite": "lax",
            "path": "/api/v1",
            "max_age": int(SESSION_TTL.total_seconds()),
        }
        response.set_cookie(SESSION_COOKIE, raw_session, **cookie_options)
        response.set_cookie(
            CSRF_COOKIE,
            raw_csrf,
            secure=settings.session_cookie_secure,
            httponly=False,
            samesite="lax",
            path="/",
            max_age=int(SESSION_TTL.total_seconds()),
        )
        return AuthSessionResponse(
            authenticated=True,
            user=AuthUserResponse(login=credential.login_name, email=user.email),
        )

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        request: Request,
        response: Response,
        principal: Principal = Depends(get_current_principal),
        session: Session = Depends(get_session),
    ):
        raw_session = request.cookies.get(SESSION_COOKIE)
        if raw_session:
            auth_session = session.get(WebAuthSession, _token_hash(raw_session))
            if auth_session and auth_session.user_id == principal.user_id:
                session.delete(auth_session)
                session.commit()
        response.delete_cookie(SESSION_COOKIE, path="/api/v1")
        response.delete_cookie(CSRF_COOKIE, path="/")
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    return router
