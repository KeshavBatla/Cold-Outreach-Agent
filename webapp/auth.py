from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence import AppUser, LocalCredential, WebAuthSession, utc_now
from webapp.database import get_session
from webapp.settings import Settings

SESSION_COOKIE = "outreach_session"
CSRF_COOKIE = "outreach_csrf"
SESSION_TTL = timedelta(hours=12)
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DUMMY_PASSWORD_HASH: str


@dataclass(frozen=True)
class Principal:
    user_id: str
    subject: str
    email: str | None = None
    identity_provider: str = "google"


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) < 12 or len(encoded) > 1024:
        raise ValueError("Password must be between 12 and 1024 UTF-8 bytes.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        encoded,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded_hash: str) -> bool:
    if len(password.encode("utf-8")) > 1024:
        return False
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded_hash.split("$")
        if algorithm != "scrypt":
            raise ValueError("Unsupported password hash.")
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt = bytes.fromhex(salt_text)
        expected = bytes.fromhex(digest_text)
        if n > 1 << 18 or r > 16 or p > 4 or len(salt) != 16 or len(expected) != 32:
            raise ValueError("Invalid password hash parameters.")
    except (ValueError, TypeError):
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=64 * 1024 * 1024,
    )
    return hmac.compare_digest(candidate, expected)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _allowed_origin(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin", "").rstrip("/")
    return bool(origin) and origin in settings.allowed_origins


def principal_from_session(
    request: Request,
    session: Session,
    *,
    require_csrf: bool = False,
) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    auth_session = session.scalar(
        select(WebAuthSession).where(
            WebAuthSession.token_hash == _token_hash(token),
            WebAuthSession.expires_at > utc_now(),
        )
    )
    if auth_session is None:
        return None
    user = session.get(AppUser, auth_session.user_id)
    credential = session.get(LocalCredential, auth_session.user_id)
    if (
        user is None
        or credential is None
        or user.identity_provider != "local"
        or user.external_subject != credential.login_name
    ):
        return None
    if require_csrf:
        settings: Settings = request.app.state.settings
        csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
        csrf_header = request.headers.get("x-csrf-token", "")
        if (
            not _allowed_origin(request, settings)
            or not csrf_cookie
            or not csrf_header
            or not hmac.compare_digest(csrf_cookie, csrf_header)
            or not hmac.compare_digest(_token_hash(csrf_header), auth_session.csrf_token_hash)
        ):
            raise HTTPException(status_code=403, detail="CSRF validation failed.")
    return Principal(
        user_id=user.id,
        subject=user.external_subject,
        email=user.email,
        identity_provider=user.identity_provider,
    )


def get_current_principal(
    request: Request,
    session: Session = Depends(get_session),
) -> Principal:
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal
    try:
        principal = principal_from_session(
            request,
            session,
            require_csrf=request.method not in {"GET", "HEAD", "OPTIONS"},
        )
    finally:
        if session.in_transaction():
            session.rollback()
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_test_principal(settings: Settings) -> Principal:
    if not settings.test_principal_enabled:
        raise HTTPException(status_code=503, detail="Test authentication is disabled.")
    return Principal(user_id="test-user", subject="test-subject")


CurrentPrincipal = Depends(get_current_principal)


_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(24))
