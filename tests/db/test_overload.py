"""Under overload, requests queue and finish: no thread-versus-connection deadlock, no leaked transaction."""

from __future__ import annotations

import asyncio

import anyio
import httpx
import pytest
from sqlalchemy import text

from radreport.core.config import get_settings
from radreport.db.session import system_session

pytestmark = pytest.mark.db


def test_many_requests_on_a_tiny_pool_and_threadpool_all_complete(migrated_db: str, two_tenants, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forty requests, two connections, four threads: the shape that used to stall every worker."""
    from radreport.api.app import create_app
    from tests.db.helpers import lab_headers

    lab, _ = two_tenants
    headers = lab_headers(migrated_db, lab, "radiologist")
    # A distinct URL, so a fresh engine is built with the tiny pool rather than reusing the suite's.
    monkeypatch.setenv("RADREPORT_DATABASE_URL", migrated_db + ("&" if "?" in migrated_db else "?") + "application_name=overload")
    monkeypatch.setenv("RADREPORT_DB__POOL_SIZE", "2")
    monkeypatch.setenv("RADREPORT_DB__MAX_OVERFLOW", "0")
    get_settings.cache_clear()
    try:
        app = create_app()

        async def run() -> list[int]:
            anyio.to_thread.current_default_thread_limiter().total_tokens = 4
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as client:
                with anyio.fail_after(45):
                    responses = await asyncio.gather(*(client.get(path, headers=headers) for path in ["/review/queue", "/review/queue/stats", "/review/metrics/usefulness"] * 14))
            return [r.status_code for r in responses]

        statuses = asyncio.run(run())
    finally:
        get_settings.cache_clear()
    assert statuses.count(200) == len(statuses), statuses
    with system_session(migrated_db) as session:
        stuck = session.execute(text("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'overload' AND state = 'idle in transaction'")).scalar_one()
    assert stuck == 0
