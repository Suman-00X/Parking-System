"""Sync routes run on the event loop with the async driver; the routes marked threaded keep the sync engine on a worker thread."""

from __future__ import annotations

import threading

import pytest

from radreport.api.routes import admin_api, admin_panel
from radreport.db import bridge
from radreport.db.session import get_sessionmaker, system_session
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db


def _spy(monkeypatch: pytest.MonkeyPatch, module: object, name: str) -> list[tuple[bool, bool, bool]]:
    """Record, for each call of module.name: bridged, session on the async driver, on a worker thread."""
    seen: list[tuple[bool, bool, bool]] = []
    real = getattr(module, name)

    def spy(session, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((bridge.in_bridge(), session.get_bind().dialect.is_async, "AnyIO worker thread" in threading.current_thread().name))
        return real(session, *args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return seen


def test_a_sync_route_runs_bridged_on_the_async_driver(migrated_db: str, two_tenants, monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _ = two_tenants
    seen = _spy(monkeypatch, admin_api, "evaluate_readiness")
    client = signed_in(make_platform_user(migrated_db))
    assert client.get(f"/admin/api/labs/{lab}/readiness").status_code == 200
    assert seen == [(True, True, False)]


def test_a_threaded_route_keeps_the_sync_engine_on_a_worker_thread(migrated_db: str, two_tenants, monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    seen = _spy(monkeypatch, admin_panel.onboarding_steps, "run_step")
    client.post(f"/admin/labs/{lab}/onboarding/steps/derive-map", follow_redirects=False)
    assert seen == [(False, False, True)]


def test_the_route_class_wraps_sync_endpoints_but_not_threaded_or_async_ones() -> None:
    from radreport.api.routes import review

    endpoints = {(route.path, method): route.endpoint for router in (admin_api.router, admin_panel.router, review.router) for route in router.routes for method in getattr(route, "methods", ())}
    assert bridge.is_bridged(endpoints[("/admin/api/labs/{tenant_id}/readiness", "GET")])
    assert bridge.is_bridged(endpoints[("/review/queue", "GET")])
    assert not bridge.is_bridged(endpoints[("/admin/labs/{tenant_id}/onboarding/steps/{step}", "POST")]), "threaded: seconds of CPU between queries"
    assert not bridge.is_bridged(endpoints[("/admin/api/labs", "GET")]), "already async"


def test_offload_runs_on_a_thread_only_when_bridged() -> None:
    import asyncio

    main = threading.get_ident()
    assert bridge.offload(threading.get_ident) == main

    async def bridged() -> tuple[int, int]:
        return await bridge.run(lambda: (threading.get_ident(), bridge.offload(threading.get_ident)))

    loop_thread, worker = asyncio.run(bridged())
    assert loop_thread == main and worker != main


def test_sessions_opened_inside_the_bridge_use_the_async_engine(migrated_db: str) -> None:
    import asyncio

    from sqlalchemy import text

    def inside() -> tuple[bool, int]:
        with system_session(migrated_db) as session:
            return get_sessionmaker(migrated_db).kw["bind"].dialect.is_async, session.execute(text("SELECT 1")).scalar_one()

    assert asyncio.run(bridge.run(inside)) == (True, 1)
    assert not get_sessionmaker(migrated_db).kw["bind"].dialect.is_async
