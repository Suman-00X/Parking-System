"""Builds the FastAPI application, mounts every route group on it, and puts the access check in front.

Order: create_app assembles the app, installs AccessMiddleware and refuses to start if the access
policy and the routes disagree; current_revision and head_revision report whether the database
schema is up to date.
"""

from __future__ import annotations

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from radreport.api.access import AccessMiddleware, RateLimiter, load_policy, verify_coverage
from radreport.api.routes import admin_api, admin_panel, ga, ingest, onboarding, review, review_ui
from radreport.core.config import get_settings
from radreport.core.logging import configure_logging
from radreport.db.session import get_engine, system_session


def current_revision() -> str | None:
    """The migration revision this database is actually at."""
    with get_engine().connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    """The newest revision this codebase knows about."""
    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    app = FastAPI(title="radreport", version="0.1.0", description=("Radiology voice-to-structured-report: the admin panel, the 15-stage V1 pipeline, and the review surface."))
    app.include_router(ingest.router)
    app.include_router(admin_api.router)
    app.include_router(onboarding.router)
    app.include_router(review.router)
    app.include_router(review_ui.router)
    app.include_router(ga.router)
    app.include_router(admin_panel.router)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        """Liveness: the process is up and serving."""
        return {"status": "ok", "environment": settings.environment}

    @app.get("/ready", tags=["ops"])
    def ready() -> JSONResponse:
        """Readiness: the dependencies this process needs are actually there."""
        checks: dict[str, str] = {}
        ok = True

        try:
            with system_session() as session:
                session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, never raise
            checks["database"] = f"{type(exc).__name__}: {exc}"[:200]
            ok = False

        try:
            revision = current_revision()
            head = head_revision()
            checks["migrations"] = f"at {revision}" if revision == head else f"at {revision}, head is {head}"
            if revision != head:
                # A process running against a schema older than its code is the
                # failure mode that presents as random column errors hours later.
                ok = False
        except Exception as exc:  # noqa: BLE001
            checks["migrations"] = f"{type(exc).__name__}: {exc}"[:200]
            ok = False

        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)

    policy = load_policy()
    verify_coverage(app, policy)
    app.add_middleware(AccessMiddleware, policy=policy, limiter=RateLimiter())
    return app


app = create_app()
