"""The admin panel's operations API: how the database is being used, read by product admins and support.

Order: query metrics (query_metrics).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from radreport.api.deps import CurrentAdmin
from radreport.db.instrumentation import METRICS

router = APIRouter(prefix="/admin/api/ops", tags=["admin-ops"])


@router.get("/queries")
def query_metrics(admin: CurrentAdmin) -> dict[str, Any]:
    """Statement-time and statements-per-request percentiles for this worker, and its heaviest routes."""
    return METRICS.snapshot()
