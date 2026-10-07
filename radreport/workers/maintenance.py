"""Platform jobs that keep the database healthy, run by the same workers as everything else.

Order: bury leases that ran out on their last attempt (reap_dead_jobs).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from radreport.workers import queue
from radreport.workers.handlers import handler
from radreport.workers.queue import ClaimedJob


@handler("reap_jobs")
async def reap_dead_jobs(session: Session, job: ClaimedJob) -> dict[str, Any]:
    """Mark poison jobs dead even when no worker is claiming their kind."""
    return {"buried": queue.reap(session)}
