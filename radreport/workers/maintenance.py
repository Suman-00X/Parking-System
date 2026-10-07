"""Platform jobs that keep the database healthy, run by the same workers as everything else.

Order: bury leases that ran out on their last attempt (reap_dead_jobs) -> keep monthly partitions
created ahead and detach months past their retention (ensure_partitions) -> refresh the materialized
canonical eval set (refresh_eval_set) -> announce cost spikes (cost_anomaly_scan) -> look for new vocabulary in every lab's edits (watch_lexicon).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from radreport.core import system_config
from radreport.workers import queue
from radreport.workers.handlers import handler
from radreport.workers.queue import ClaimedJob

#: Tables split into monthly partitions, and the retention setting each may be trimmed by.
#: audit_log and edit_event have none on purpose: the audit trail is append-only, and edit events are the training corpus.
MONTHLY_TABLES: dict[str, str | None] = {"asr_segment": "retention.asr_segment_months", "edit_event": None, "audit_log": None, "stage_execution": "retention.stage_execution_months"}


def month_bounds(anchor: dt.date, offset: int) -> tuple[str, dt.date, dt.date]:
    """(`yYYYYmMM` suffix, first day, first day of the next month) for the month `offset` months from `anchor`."""
    month = anchor.month - 1 + offset
    year = anchor.year + month // 12
    month = month % 12 + 1
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), month % 12 + 1, 1)
    return f"y{start:%Y}m{start:%m}", start, end


def ensure_partitions_now(session: Session, *, today: dt.date | None = None) -> dict[str, Any]:
    """Create this month and the configured months ahead for every monthly table; detach what retention says to."""
    anchor = (today or dt.date.today()).replace(day=1)
    ahead = int(system_config.resolve(session, "partitions.months_ahead").value)
    created: list[str] = []
    archived: list[str] = []
    for table, retention_key in MONTHLY_TABLES.items():
        for offset in range(0, ahead + 1):
            suffix, start, end = month_bounds(anchor, offset)
            exists = session.execute(text("SELECT 1 FROM pg_class WHERE relname = :n"), {"n": f"{table}_{suffix}"}).first()
            session.execute(text("SELECT ensure_month_partition(:p, :s, :a, :b)"), {"p": table, "s": suffix, "a": start, "b": end})
            if not exists:
                created.append(f"{table}_{suffix}")
        months = int(system_config.resolve(session, retention_key).value) if retention_key else 0
        if months > 0:
            _suffix, keep_from, _end = month_bounds(anchor, -months)
            archived += list(session.execute(text("SELECT detach_month_partitions(:p, :k)"), {"p": table, "k": keep_from}).scalars().all())
    return {"created": created, "archived": archived, "months_ahead": ahead}


@handler("reap_jobs")
async def reap_dead_jobs(session: Session, job: ClaimedJob) -> dict[str, Any]:
    """Mark poison jobs dead even when no worker is claiming their kind."""
    return {"buried": queue.reap(session)}


@handler("ensure_partitions")
async def ensure_partitions(session: Session, job: ClaimedJob) -> dict[str, Any]:
    """Keep monthly partitions ahead of the calendar; without this, rows fall into the default partition after the first year."""
    return ensure_partitions_now(session)


@handler("refresh_eval_set")
async def refresh_eval_set(session: Session, job: ClaimedJob) -> dict[str, Any]:
    """Refresh mv_canonical_eval_set; concurrent, so readers keep the old copy until the new one is ready."""
    return {"rows": int(session.execute(text("SELECT refresh_canonical_eval_set()")).scalar_one())}


@handler("cost_anomaly_scan")
async def cost_anomaly_scan(session: Session, job: ClaimedJob) -> dict[str, Any]:
    """Look for labs whose daily spend jumped, and announce each new spike once."""
    from radreport.monitoring.costs import scan_for_anomalies

    return {"announced": scan_for_anomalies(session)}


@handler("watch_lexicon")
async def watch_lexicon(session: Session, job: ClaimedJob) -> dict[str, Any]:
    """Scan every lab's new edits for vocabulary its lexicon lacks; each lab in its own bound session."""
    from sqlalchemy import select

    from radreport.core.types import TenantStatus
    from radreport.db.models.tenancy import Tenant
    from radreport.db.session import tenant_session
    from radreport.onboarding.term_watch import scan_edits

    labs = list(session.execute(select(Tenant.id).where(Tenant.status.in_((TenantStatus.ONBOARDING, TenantStatus.PILOT, TenantStatus.LIVE)))).scalars())
    found = 0
    for lab in labs:
        with tenant_session(lab) as lab_session:
            found += scan_edits(lab_session, lab)["unknown_terms"]
    return {"labs": len(labs), "unknown_terms": found}
