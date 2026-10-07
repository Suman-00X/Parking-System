"""The worker loop: claim jobs, run each in a session bound to its own lab, and record what happened.

Order: lease ready work (Worker.run_once -> queue.claim) -> keep the lease alive while a job runs
(_Heartbeat) -> run the handler and mark the job done in the same transaction as its writes
(_process) -> on an error, roll the handler's writes back and record the failure in a fresh
session (queue.fail) -> poll with backoff when the queue is empty (Worker.run_forever).
"""

from __future__ import annotations

import asyncio
import os
import random
import socket
import threading
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.db.instrumentation import query_scope
from radreport.db.session import system_session, tenant_session
from radreport.workers import handlers, queue
from radreport.workers.queue import ClaimedJob

log = get_logger(__name__)


@contextmanager
def job_session(job: ClaimedJob, url: str | None = None) -> Iterator[Session]:
    """A lab's job runs bound to that lab, so row-level security applies to everything the handler touches."""
    if job.tenant_id is None:
        with system_session(url) as session:
            yield session
    else:
        with tenant_session(job.tenant_id, url=url) as session:
            yield session


class _Heartbeat:
    """Renews a job's lease on a timer, from its own session, until stopped."""

    def __init__(self, job: ClaimedJob, *, worker_id: str, visibility_seconds: int, url: str | None) -> None:
        self._job, self._worker, self._visibility, self._url = job, worker_id, visibility_seconds, url
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"lease-{job.id}")

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(max(1.0, self._visibility / 3)):
            try:
                with job_session(self._job, self._url) as session:
                    if not queue.extend_lease(session, self._job, worker_id=self._worker, visibility_seconds=self._visibility):
                        log.warning("job_lease_lost", job_id=str(self._job.id))
                        return
            except Exception as exc:  # noqa: BLE001 - a missed renewal is retried on the next beat
                log.warning("job_lease_renewal_failed", job_id=str(self._job.id), error=type(exc).__name__)


class Worker:
    """Drains the queue for a set of job kinds."""

    def __init__(self, *, kinds: list[str] | None = None, worker_id: str | None = None, concurrency: int = 1, visibility_seconds: int = 300, poll_min_seconds: float = 0.5, poll_max_seconds: float = 5.0, url: str | None = None) -> None:
        self.kinds = kinds or handlers.kinds()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.concurrency = concurrency
        self.visibility_seconds = visibility_seconds
        self.poll_min, self.poll_max = poll_min_seconds, poll_max_seconds
        self.url = url
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self) -> int:
        """Claim and run up to `concurrency` jobs; returns how many were claimed."""
        with system_session(self.url) as session:
            claimed = queue.claim(session, worker_id=self.worker_id, kinds=self.kinds, limit=self.concurrency, visibility_seconds=self.visibility_seconds)
        if claimed:
            await asyncio.gather(*(self._process(job) for job in claimed))
        return len(claimed)

    async def run_forever(self) -> None:
        """Poll until stopped, backing off while the queue is empty."""
        delay = self.poll_min
        log.info("worker_started", worker_id=self.worker_id, kinds=self.kinds, concurrency=self.concurrency)
        while not self._stopping.is_set():
            try:
                worked = await self.run_once()
            except Exception as exc:  # noqa: BLE001 - a database blip must not kill the worker
                log.error("worker_claim_failed", worker_id=self.worker_id, error=f"{type(exc).__name__}: {exc}"[:300])
                worked = 0
            delay = self.poll_min if worked else min(self.poll_max, delay * 2)
            if not worked:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay * random.uniform(0.8, 1.2))
                except TimeoutError:
                    pass
        log.info("worker_stopped", worker_id=self.worker_id)

    async def _process(self, job: ClaimedJob) -> None:
        log.info("job_started", job_id=str(job.id), kind=job.kind, attempt=job.attempts, tenant_id=str(job.tenant_id) if job.tenant_id else None)
        try:
            run = handlers.get(job.kind)
            with _Heartbeat(job, worker_id=self.worker_id, visibility_seconds=self.visibility_seconds, url=self.url), query_scope(f"job:{job.kind}"), job_session(job, self.url) as session:
                result = await run(session, job)
                session.flush()
                # Done in the same transaction as the handler's own writes: both commit, or neither does.
                if not queue.complete(session, job, worker_id=self.worker_id, result=result):
                    raise RuntimeError("lease was lost to another worker before the job finished")
            log.info("job_succeeded", job_id=str(job.id), kind=job.kind)
        except Exception as exc:  # noqa: BLE001 - any failure is recorded on the job
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
            try:
                with job_session(job, self.url) as session:
                    queue.fail(session, job, worker_id=self.worker_id, error=detail)
            except Exception as record_exc:  # noqa: BLE001 - the lease expires and another worker retries
                log.error("job_failure_not_recorded", job_id=str(job.id), error=type(record_exc).__name__)
