from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from webapp.settings import Settings


@dataclass(frozen=True)
class Principal:
    user_id: str
    subject: str
    email: str | None = None


def get_current_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
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
