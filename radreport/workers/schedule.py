"""Periodic platform jobs, queued once per period however many workers are running.

Order: the jobs and how often each runs (PERIODIC) -> queue those whose period has no job yet
(enqueue_due), which every worker calls about once a minute; the period number in the dedupe key
is what keeps it to one job per period.
"""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.db.models.jobs import Job
from radreport.workers.queue import enqueue

#: kind -> seconds between runs.
PERIODIC: dict[str, int] = {"reap_jobs": 600, "ensure_partitions": 6 * 3600, "cost_anomaly_scan": 6 * 3600, "refresh_eval_set": 24 * 3600, "watch_lexicon": 24 * 3600}


def enqueue_due(session: Session, *, now: float | None = None) -> list[str]:
    """Queue every periodic job not yet queued for its current period; returns the kinds queued."""
    moment = now if now is not None else time.time()
    queued: list[str] = []
    for kind, every in PERIODIC.items():
        bucket = f"{kind}:{int(moment // every)}"
        if session.execute(select(Job.id).where(Job.tenant_id.is_(None), Job.kind == kind, Job.dedupe_key == bucket)).first():
            continue
        if enqueue(session, kind, {}, dedupe_key=bucket, max_attempts=3) is not None:
            queued.append(kind)
    return queued
