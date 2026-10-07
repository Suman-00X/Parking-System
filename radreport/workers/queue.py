"""The job queue's operations, on top of the job table and its claim_jobs() function.

Order: put work on the queue inside the caller's own transaction (enqueue) -> a worker leases
ready work across every lab (claim) -> keeps a long job's lease alive (extend_lease) -> records
the outcome from a session bound to the job's lab (complete, fail; fail retries with backoff until
the attempts run out, then the job is dead) -> buries leases that ran out on their last attempt
(reap).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import JobStatus
from radreport.db.models.jobs import Job

log = get_logger(__name__)

#: Retry delays in seconds, by attempt; the last one repeats.
BACKOFF_SECONDS = (5, 30, 120, 600, 1800)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """What a worker holds after claiming: enough to bind the right lab and run the handler."""

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(session: Session, kind: str, payload: dict[str, Any] | None = None, *, tenant_id: uuid.UUID | None = None, run_at: dt.datetime | None = None, priority: int = 0, max_attempts: int = 5, dedupe_key: str | None = None) -> uuid.UUID | None:
    """Queue a job in the caller's transaction, so it exists exactly when the change that caused it commits.

    With a dedupe key, a second live job with the same (lab, kind, key) is not queued and None is returned.
    """
    job_id = uuid.uuid4()
    values: dict[str, Any] = {"id": job_id, "tenant_id": tenant_id, "kind": kind, "payload": payload or {}, "priority": priority, "max_attempts": max_attempts, "dedupe_key": dedupe_key}
    if run_at is not None:
        values["run_at"] = run_at
    statement = insert(Job).values(**values)
    if dedupe_key is not None:
        statement = statement.on_conflict_do_nothing()
    inserted = session.execute(statement.returning(Job.id)).scalar_one_or_none()
    if inserted is None:
        log.info("job_deduplicated", kind=kind, dedupe_key=dedupe_key)
    return inserted


def claim(session: Session, *, worker_id: str, kinds: list[str], limit: int = 1, visibility_seconds: int = 300) -> list[ClaimedJob]:
    """Lease up to `limit` ready jobs; two workers never receive the same one."""
    rows = session.execute(text("SELECT id, tenant_id, kind, payload, attempts, max_attempts FROM claim_jobs(:w, CAST(:k AS text[]), :n, :v)"), {"w": worker_id, "k": kinds, "n": limit, "v": visibility_seconds}).all()
    return [ClaimedJob(id=r[0], tenant_id=r[1], kind=r[2], payload=r[3] or {}, attempts=r[4], max_attempts=r[5]) for r in rows]


def extend_lease(session: Session, job: ClaimedJob, *, worker_id: str, visibility_seconds: int = 300) -> bool:
    """Push the lease out while a long job is still working; False if another worker has taken it."""
    result = session.execute(update(Job).where(Job.id == job.id, Job.locked_by == worker_id, Job.status == JobStatus.RUNNING).values(locked_until=text(f"now() + interval '{int(visibility_seconds)} seconds'")))
    return bool(result.rowcount)  # type: ignore[attr-defined]


def complete(session: Session, job: ClaimedJob, *, worker_id: str, result: dict[str, Any] | None = None) -> bool:
    """Mark the job done; False if the lease was lost to another worker first."""
    outcome = session.execute(update(Job).where(Job.id == job.id, Job.locked_by == worker_id, Job.status == JobStatus.RUNNING).values(status=JobStatus.SUCCEEDED, finished_at=text("now()"), locked_by=None, locked_until=None, result=result))
    return bool(outcome.rowcount)  # type: ignore[attr-defined]


def fail(session: Session, job: ClaimedJob, *, worker_id: str, error: str) -> str:
    """Record a failure: back on the queue after a delay, or dead once the attempts are used up. Returns the new status."""
    if job.attempts >= job.max_attempts:
        values: dict[str, Any] = {"status": JobStatus.DEAD, "finished_at": text("now()")}
    else:
        delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
        values = {"status": JobStatus.QUEUED, "run_at": text(f"now() + interval '{delay} seconds'")}
    session.execute(update(Job).where(Job.id == job.id, Job.locked_by == worker_id).values(**values, locked_by=None, locked_until=None, last_error=error[:2000]))
    log.warning("job_failed", job_id=str(job.id), kind=job.kind, attempt=job.attempts, max_attempts=job.max_attempts, next_status=values["status"], error=error[:200])
    return str(values["status"])


def reap(session: Session) -> int:
    """Bury every lease that expired on its final attempt."""
    return int(session.execute(text("SELECT reap_jobs()")).scalar_one())
