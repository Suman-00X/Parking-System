"""work_backlog(): the metrics scrape counts every lab's queued work without being able to read any of it."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text

from radreport.db.models.jobs import Job
from radreport.db.session import system_session, tenant_session
from radreport.observability import metrics as prom
from radreport.workers import queue

pytestmark = pytest.mark.db

KIND = f"test_backlog_{uuid.uuid4().hex[:6]}"


def _backlog(db: str) -> dict[tuple[str, str], int]:
    with system_session(db) as session:
        return {(kind, state): items for source, kind, state, items, _ in session.execute(text("SELECT * FROM work_backlog()")) if source == "job"}


def test_the_scrape_counts_every_labs_jobs_but_cannot_read_them(migrated_db: str, two_tenants) -> None:
    lab_a, lab_b = two_tenants
    try:
        with tenant_session(lab_a, url=migrated_db) as session:
            for n in range(3):
                queue.enqueue(session, KIND, {"n": n}, tenant_id=lab_a)
        with tenant_session(lab_b, url=migrated_db) as session:
            queue.enqueue(session, KIND, {"n": 9}, tenant_id=lab_b)

        assert _backlog(migrated_db)[(KIND, "queued")] == 4
        # The same unbound session sees none of the rows themselves.
        with system_session(migrated_db) as session:
            assert session.execute(select(func.count()).select_from(Job).where(Job.kind == KIND)).scalar_one() == 0
    finally:
        for lab in (lab_a, lab_b):
            with tenant_session(lab, url=migrated_db) as session:
                session.execute(text("DELETE FROM job WHERE kind = :k"), {"k": KIND})


def test_the_collector_reads_the_function(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with system_session(migrated_db) as session:
        queue.enqueue(session, KIND, {"n": 1})
    try:
        body, _ = prom.render()
        assert f'radreport_jobs_backlog{{kind="{KIND}",state="queued"}} 1.0' in body.decode()
    finally:
        with system_session(migrated_db) as session:
            session.execute(text("DELETE FROM job WHERE kind = :k"), {"k": KIND})
