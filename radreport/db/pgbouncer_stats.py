"""PgBouncer's own view of its pools, read from its admin console, for the admin panel's pool dashboard.

Order: find the admin console (admin_url: RADREPORT_DB__PGBOUNCER_ADMIN_URL, or the app's database
URL with the database swapped for the `pgbouncer` virtual one) -> read SHOW POOLS and the pool
limits from SHOW CONFIG (pool_report) -> flag pools with clients waiting or servers near the limit.

The console speaks only the simple query protocol and has no transactions, so it is read with a
plain autocommit psycopg connection rather than through the SQLAlchemy engine. The connecting role
must be listed in PgBouncer's `stats_users`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import psycopg
from sqlalchemy.engine import make_url

from radreport.core.config import get_settings

#: The share of a pool's server connections in use above which the dashboard marks it busy.
BUSY_SHARE = 0.8

_LIMITS = ("default_pool_size", "reserve_pool_size", "max_client_conn", "pool_mode")


@dataclass(frozen=True, slots=True)
class Pool:
    database: str
    user: str
    clients_active: int
    clients_waiting: int
    servers_active: int
    servers_idle: int
    servers_used: int
    max_wait_ms: float
    pool_mode: str
    busy: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def admin_url() -> str | None:
    """The admin console to read, or None when the app is not behind PgBouncer."""
    db = get_settings().db
    if db.pgbouncer_admin_url:
        return db.pgbouncer_admin_url
    if not db.pgbouncer:
        return None
    return make_url(get_settings().database_url).set(drivername="postgresql", database="pgbouncer").render_as_string(hide_password=False)


def pool_report(url: str | None = None) -> dict[str, Any]:
    """Every pool PgBouncer runs for the app, its limits, and which pools are waiting or busy."""
    target = url or admin_url()
    if target is None:
        return {"enabled": False, "reason": "not behind PgBouncer (RADREPORT_DB__PGBOUNCER is off)", "pools": []}
    try:
        with psycopg.connect(target, autocommit=True, prepare_threshold=None, connect_timeout=3) as conn:
            pools = _rows(conn.execute("SHOW POOLS"))
            config = {row["key"]: row["value"] for row in _rows(conn.execute("SHOW CONFIG")) if row["key"] in _LIMITS}
    except psycopg.Error as exc:
        return {"enabled": True, "reachable": False, "reason": f"cannot read the PgBouncer admin console: {exc.__class__.__name__}", "pools": []}
    size = int(config.get("default_pool_size", 0) or 0)
    reserve = int(config.get("reserve_pool_size", 0) or 0)
    out = [_pool(row, size) for row in pools if row["database"] != "pgbouncer"]
    return {"enabled": True, "reachable": True, "limits": {"default_pool_size": size, "reserve_pool_size": reserve, "max_client_conn": int(config.get("max_client_conn", 0) or 0), "pool_mode": config.get("pool_mode")}, "busy_share": BUSY_SHARE, "waiting": [p.database for p in out if p.clients_waiting], "busy": [p.database for p in out if p.busy], "pools": [p.as_dict() for p in out]}


def _rows(cursor: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    names = [d.name for d in cursor.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _pool(row: dict[str, Any], size: int) -> Pool:
    active = int(row["sv_active"])
    wait_ms = int(row.get("maxwait", 0)) * 1000.0 + int(row.get("maxwait_us", 0)) / 1000.0
    return Pool(database=row["database"], user=row["user"], clients_active=int(row["cl_active"]), clients_waiting=int(row["cl_waiting"]), servers_active=active, servers_idle=int(row["sv_idle"]), servers_used=int(row["sv_used"]), max_wait_ms=round(wait_ms, 1), pool_mode=row["pool_mode"], busy=bool(size) and active >= BUSY_SHARE * size)
