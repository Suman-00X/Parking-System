"""The async session layer: the same lab isolation and read-only guard as the sync one, and the endpoints moved onto it."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from radreport.db.async_session import async_read_session, async_system_session, async_tenant_session
from radreport.db.models.jobs import Job
from radreport.db.session import tenant_session
from radreport.workers.queue import enqueue

pytestmark = pytest.mark.db


def test_an_async_lab_session_sees_only_its_lab(migrated_db: str, two_tenants) -> None:
    lab_a, lab_b = two_tenants
    with tenant_session(lab_a, url=migrated_db) as session:
        job_id = enqueue(session, "test_async_probe", tenant_id=lab_a)

    async def read(lab: uuid.UUID) -> list[uuid.UUID]:
        async with async_tenant_session(lab, url=migrated_db) as session:
            return list((await session.execute(select(Job.id).where(Job.id == job_id))).scalars())

    assert asyncio.run(read(lab_a)) == [job_id]
    assert asyncio.run(read(lab_b)) == []
    with tenant_session(lab_a, url=migrated_db) as session:
        session.execute(text("DELETE FROM job WHERE id = :id"), {"id": job_id})


def test_an_async_read_session_refuses_writes() -> None:
    async def write() -> None:
        async with async_read_session() as session:
            await session.execute(text("DELETE FROM rate_limit_counter"))

    with pytest.raises(DBAPIError, match="read-only"):
        asyncio.run(write())


def test_the_system_session_binds_no_lab(migrated_db: str) -> None:
    async def scope() -> str:
        async with async_system_session(migrated_db) as session:
            return (await session.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar_one()

    assert asyncio.run(scope()) == ""


def test_the_moved_endpoints_still_answer(migrated_db: str, two_tenants) -> None:
    from fastapi.testclient import TestClient

    from radreport.api.app import create_app
    from tests.db.helpers import lab_headers

    lab, _ = two_tenants
    client = TestClient(create_app())
    for _ in range(3):  # several requests, so pooled connections are reused across them
        assert client.get("/health").json()["checks"]["database"]["ok"]
        listing = client.get("/ingest/recordings", headers=lab_headers(migrated_db, lab, "radiologist"))
        assert listing.status_code == 200 and listing.headers["x-total-count"] == "0"
