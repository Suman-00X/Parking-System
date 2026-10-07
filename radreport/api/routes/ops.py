"""The admin panel's operations API: how the database is being used, and the thresholds ops may change.

Order: query metrics (query_metrics) -> table health, vacuum and bloat (table_health) ->
spend per lab and per stage (cost_summary) -> operational settings (list_settings, set_setting,
reset_setting).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from radreport.admin.auth import AuthenticatedAdmin
from radreport.api.deps import CurrentAdmin, admin_lab_session, client_ip
from radreport.core import system_config
from radreport.db.instrumentation import METRICS
from radreport.db.session import read_session, system_session

router = APIRouter(prefix="/admin/api/ops", tags=["admin-ops"])


@router.get("/queries")
def query_metrics(admin: CurrentAdmin) -> dict[str, Any]:
    """Statement-time and statements-per-request percentiles for this worker, and its heaviest routes."""
    return METRICS.snapshot()


@router.get("/tables")
def table_health(admin: CurrentAdmin) -> dict[str, Any]:
    """Dead rows, last autovacuum and size per table, with the bloated ones flagged."""
    from radreport.db.table_health import BLOAT_RATIO, table_stats

    with read_session() as session:
        stats = table_stats(session)
    return {"bloat_ratio_threshold": BLOAT_RATIO, "bloated": [t.table for t in stats if t.bloated], "tables": [t.as_dict() for t in stats]}


cost_router = APIRouter(prefix="/admin/api", tags=["admin-ops"])


@cost_router.get("/costs")
def cost_summary(admin: CurrentAdmin, tenant_id: uuid.UUID | None = None, days: int = 30) -> dict[str, Any]:
    """Spend over the last `days` (7, 30 or 90): across labs, or for one lab by stage, with spikes."""
    from radreport.monitoring import costs

    if days not in (7, 30, 90):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "days must be 7, 30 or 90")
    with read_session() as session:
        summary = costs.lab_summary(session, tenant_id, days=days) if tenant_id else costs.platform_summary(session, days=days)
    out = {**summary, "start": summary["start"].isoformat(), "end": summary["end"].isoformat(), "series": [{"day": d.isoformat(), "cost_usd": v} for d, v in summary["series"]], "spikes": [s.as_dict() for s in summary["spikes"]]}
    if "labs" in out:
        out["labs"] = [{"tenant_id": str(lab.tenant_id), "name": lab.name, "cost_usd": round(lab.cost_usd, 4), "runs": lab.runs, "avg_cost_usd": round(lab.avg_cost_usd, 4), "previous_cost_usd": round(lab.previous_cost_usd, 4), "change": lab.change, "failed_runs": lab.failed_runs, "budget_hits": lab.budget_hits, "spikes": [s.as_dict() for s in lab.spikes]} for lab in summary["labs"]]
    else:
        out["tenant_id"] = str(tenant_id)
    return out


@contextmanager
def config_session(admin: AuthenticatedAdmin, tenant_id: uuid.UUID | None, request: Request) -> Iterator[Session]:
    """Platform-wide values are written from a session bound to no lab; a lab's override from one bound to it."""
    if tenant_id is None:
        with system_session() as session:
            yield session
    else:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            yield session


def resolved_out(r: system_config.Resolved) -> dict[str, Any]:
    spec = system_config.SETTINGS[r.key]
    return {"key": r.key, "label": spec.label, "description": spec.description, "group": spec.group, "value": r.value, "source": r.source, "platform_value": r.platform_value, "lab_value": r.lab_value, "default": spec.kind(spec.default), "env_var": spec.env_var, "minimum": spec.minimum, "maximum": spec.maximum, "type": "int" if spec.kind is int else "number"}


@router.get("/config")
def list_settings(admin: CurrentAdmin, request: Request, tenant_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Every operational setting's effective value, platform-wide or for one lab, and where it came from."""
    with config_session(admin, tenant_id, request) as session:
        return [resolved_out(r) for r in system_config.resolve_all(session, tenant_id=tenant_id)]


class SettingRequest(BaseModel):
    value: float
    tenant_id: uuid.UUID | None = None


@router.post("/config/{key}")
def set_setting(key: str, body: SettingRequest, admin: CurrentAdmin, request: Request) -> dict[str, Any]:
    """Store a value platform-wide, or for one lab when tenant_id is given."""
    try:
        with config_session(admin, body.tenant_id, request) as session:
            return resolved_out(system_config.set_value(session, key, body.value, actor_id=admin.platform_user_id, tenant_id=body.tenant_id))
    except system_config.SettingRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


class ResetRequest(BaseModel):
    tenant_id: uuid.UUID | None = None


@router.post("/config/{key}/reset")
def reset_setting(key: str, body: ResetRequest, admin: CurrentAdmin, request: Request) -> dict[str, Any]:
    """Remove a stored value so the next level (platform, environment, default) applies."""
    try:
        with config_session(admin, body.tenant_id, request) as session:
            return resolved_out(system_config.reset_value(session, key, actor_id=admin.platform_user_id, tenant_id=body.tenant_id))
    except system_config.SettingRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
