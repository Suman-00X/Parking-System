"""Builds the FastAPI application, mounts every route group on it, and puts the access check in front.

Order: create_app assembles the app, refuses to start if the access policy disagrees with the
routes or their parameters, and installs AccessMiddleware (who may call) in front of
InputValidationMiddleware (with what), all inside RequestCacheMiddleware (one lookup per request)
and QueryMetricsMiddleware (how many statements); current_revision and head_revision report
whether the database schema is up to date.
"""

from __future__ import annotations

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from radreport.api.access import AccessMiddleware, RateLimiter, load_policy, verify_coverage
from radreport.api.input_check import InputValidationMiddleware, verify_params
from radreport.api.read_your_writes import ReadYourWritesMiddleware
from radreport.api.routes import admin_api, admin_ops_panel, admin_panel, auth, ga, health, ingest, onboarding, ops, review, review_ui
from radreport.auth.lab import require_token_secret
from radreport.cache import shared as shared_cache
from radreport.cache.request import RequestCacheMiddleware
from radreport.core.config import get_settings
from radreport.core.logging import configure_logging
from radreport.db.instrumentation import QueryMetricsMiddleware
from radreport.db.session import get_engine, system_session


def current_revision() -> str | None:
    """The migration revision this database is actually at."""
    with get_engine().connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    """The newest revision this codebase knows about."""
    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


def _replica_health() -> dict[str, object]:
    from radreport.db.session import replica_lag_seconds

    settings = get_settings().db
    assert settings.replica_url
    lag = replica_lag_seconds(settings.replica_url)
    return {"ok": lag <= settings.replica_max_lag_seconds, "lag_seconds": round(lag, 3), "max_lag_seconds": settings.replica_max_lag_seconds}


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(title="radreport", version="0.1.0", description=("Radiology voice-to-structured-report: the admin panel, the 15-stage V1 pipeline, and the review surface."))
    app.include_router(auth.router)
    app.include_router(ingest.router)
    app.include_router(admin_api.router)
    app.include_router(ops.router)
    app.include_router(ops.cost_router)
    app.include_router(onboarding.router)
    app.include_router(review.router)
    app.include_router(review_ui.router)
    app.include_router(ga.router)
    app.include_router(admin_panel.router)
    app.include_router(admin_ops_panel.router)

    app.include_router(health.router)
    shared_cache.register_health()
    if get_settings().db.replica_url:
        health.register_check("replica", _replica_health)

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

        extra_ok, extra = health.readiness_checks()
        checks.update(extra)
        ok = ok and extra_ok
        return JSONResponse({"status": "ready" if ok else "not_ready", "instance_id": health.instance_id(), "checks": checks}, status_code=200 if ok else 503)

    require_token_secret()
    policy = load_policy()
    verify_coverage(app, policy)
    verify_params(app, policy)
    # Added last runs first: the caller is identified and authorized before any body is read.
    app.add_middleware(InputValidationMiddleware)
    app.add_middleware(AccessMiddleware, policy=policy, limiter=RateLimiter())
    app.add_middleware(RequestCacheMiddleware)
    app.add_middleware(ReadYourWritesMiddleware)
    # Outermost, so the statements the access check itself runs are counted against the request too.
    app.add_middleware(QueryMetricsMiddleware)
    app.add_middleware(health.InstanceIdMiddleware)
    return app


app = create_app()
