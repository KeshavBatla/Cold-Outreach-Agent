from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from webapp.database import database_ready, get_session, initialize_local_schema, session_factory
from webapp.auth import Principal
from webapp.auth_routes import build_auth_router
from webapp.gmail_routes import build_gmail_router
from webapp.routes import build_api_router
from webapp.schemas import HealthResponse
from webapp.settings import Settings

logger = logging.getLogger("cold_outreach.api")


def create_app(
    *,
    settings: Settings | None = None,
    sessions: sessionmaker | None = None,
    readiness_check: Callable[[], bool] | None = None,
) -> FastAPI:
    application_settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        initialize_local_schema(application_settings)
        yield

    application = FastAPI(
        title="Cold Outreach API",
        version="1.0.0",
        description="Owner-scoped research, review, and explicit Gmail delivery API.",
        lifespan=lifespan,
    )
    application.state.settings = application_settings
    application.state.session_factory = sessions or session_factory(application_settings)
    def override_session():
        with application.state.session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    application.state.readiness_check = readiness_check or (
        lambda: database_ready(application_settings)
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(application_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-CSRF-Token",
            "Last-Event-ID",
            "X-Webapp-Test-Auth",
        ],
    )

    @application.exception_handler(Exception)
    async def safe_internal_error(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error("request_failed request_id=%s path=%s", request_id, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error.", "request_id": request_id},
        )

    @application.middleware("http")
    async def assign_request_id(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        if (
            application_settings.test_principal_enabled
            and application_settings.app_environment in {"development", "test"}
            and application_settings.database_url.startswith("sqlite:")
            and request.headers.get("x-webapp-test-auth") == "enabled"
        ):
            request.state.principal = Principal(
                user_id="test-user",
                subject="local-test-only",
                email="test@example.invalid",
            )
        return await call_next(request)

    @application.get("/health/live", response_model=HealthResponse, tags=["health"])
    def liveness():
        return HealthResponse(status="ok", database="available")

    @application.get("/health/ready", response_model=HealthResponse, tags=["health"])
    def readiness():
        try:
            ready = bool(application.state.readiness_check())
        except Exception:
            ready = False
        return HealthResponse(
            status="ok" if ready else "not_ready",
            database="available" if ready else "unavailable",
        )

    application.include_router(build_auth_router())
    application.include_router(build_gmail_router())
    application.include_router(build_api_router())
    return application


app = create_app()
