"""Opens database connections and binds each one to a single lab, so a query cannot reach another lab's rows.

Order: get the engine and session factory (get_engine, get_sessionmaker; inside bridged request
code, db/bridge.py, both are the async engine's) -> open a unit of work
for one lab (tenant_session) or for the system (system_session) -> or a read-only one on the
replica when one is configured, caught up, and this browser has not just written (read_session,
replica_url_for_reads). bind_tenant and select_org are the lower-level pieces those use.
"""

from __future__ import annotations

import contextvars
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from radreport.core.config import get_settings
from radreport.core.errors import CrossTenantAccess, NoTenantContext
from radreport.core.logging import get_logger
from radreport.core.tenancy import PRINCIPAL_GUC, TENANT_GUC, Principal, current_tenant_id_or_none, tenant_scope
from radreport.db import instrumentation, sharding
from radreport.db.bridge import in_bridge

_log = get_logger(__name__)

#: `session.info` key naming the product admin acting through a session, if any.
ACTING_PLATFORM_USER = "acting_platform_user_id"


def engine_options() -> dict[str, Any]:
    """The pool and driver options every engine is created with, from settings."""
    db = get_settings().db
    options: dict[str, Any] = {"pool_pre_ping": True, "pool_size": db.pool_size, "max_overflow": db.max_overflow, "pool_timeout": db.pool_timeout_seconds, "pool_recycle": db.pool_recycle_seconds, "echo": db.echo if db.echo is not None else get_settings().environment == "test", "future": True}
    connect_args: dict[str, Any] = {"connect_timeout": db.connect_timeout_seconds}
    if db.pgbouncer:
        # Transaction pooling hands each transaction any server connection, so a prepared statement may not exist there.
        connect_args["prepare_threshold"] = None
    options["connect_args"] = connect_args
    return options


def get_engine(url: str | None = None) -> Engine:
    """The engine for `url`; inside bridged request code, the async engine's sync face, so its queries await instead of blocking."""
    if in_bridge():
        from radreport.db.async_session import get_async_engine  # local: async_session imports this module

        return get_async_engine(url).sync_engine
    # Keyed on the resolved URL, so a changed database_url never returns an engine built for the old one.
    return _sync_engine(url or get_settings().database_url)


@lru_cache(maxsize=16)
def _sync_engine(url: str) -> Engine:
    instrumentation.install()
    sharding.install_tenant_sync()
    # Tenant scope lives in `SET LOCAL`, so a connection handed back to the pool carries nothing.
    engine = create_engine(url, **engine_options())
    event.listen(engine, "first_connect", _check_connection_budget)
    return engine


def connection_budget(max_connections: int) -> tuple[int, int]:
    """(connections every worker's pools could open together, what the server allows)."""
    db = get_settings().db
    # Request sessions pass one admission gate of pool_size + max_overflow, on whichever driver; the other engine may keep pool_size idle.
    per_worker = db.pool_size + db.max_overflow + db.pool_size + db.side_pool_size + 2
    return per_worker * db.workers_hint, max_connections


def _check_connection_budget(dbapi_connection: Any, _record: Any) -> None:
    """Warn once per engine when the pools could exhaust the server: the failure is "too many clients" under peak load, not at start-up."""
    if get_settings().db.pgbouncer:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SHOW max_connections")
        allowed = int(cursor.fetchone()[0])
    finally:
        cursor.close()
    wanted, allowed = connection_budget(allowed)
    if wanted > allowed * 0.9:
        _log.warning("connection_budget_exceeded", pools_could_open=wanted, max_connections=allowed, detail="lower RADREPORT_DB__POOL_SIZE, raise max_connections, or put PgBouncer in front (RADREPORT_DB__PGBOUNCER=true)")


get_engine.cache_clear = _sync_engine.cache_clear  # type: ignore[attr-defined]


def get_side_engine(url: str | None = None) -> Engine:
    """A small pool for the access middleware's own queries (rate limits, admin sign-in), so they never wait behind request sessions for a connection."""
    if in_bridge():
        from radreport.db.async_session import get_async_engine

        return get_async_engine(url, side=True).sync_engine
    return _sync_side_engine(url or get_settings().database_url)


def side_pool_options() -> dict[str, Any]:
    """engine_options with the side pool's size."""
    return {**engine_options(), "pool_size": get_settings().db.side_pool_size, "max_overflow": 2}


@lru_cache(maxsize=4)
def _sync_side_engine(url: str) -> Engine:
    return create_engine(url, **side_pool_options())


@contextmanager
def side_session() -> Iterator[Session]:
    """A lab-less unit of work on the side pool."""
    with sessionmaker(bind=get_side_engine(), expire_on_commit=False, future=True)() as session:
        _bind_scope(session, None, None)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


#: `session.info` keys: the scope to apply at the start of each transaction, and whether it is read-only.
SCOPE = "radreport_scope"
READ_ONLY = "radreport_read_only"
_SCOPE_SQL = text(f"SELECT set_config('{TENANT_GUC}', :tid, true), set_config('{PRINCIPAL_GUC}', :kind, true)")


def _scope_params(tenant_id: uuid.UUID | None, principal: Principal | None) -> dict[str, str]:
    return {"tid": str(tenant_id) if tenant_id else "", "kind": principal.kind if principal else "system"}


def _bind_scope(session: Session, tenant_id: uuid.UUID | None, principal: Principal | None, *, read_only: bool = False) -> None:
    """Set the GUCs the RLS policies read: now, if a transaction is open, and at the start of every later one.

    Not by opening a transaction here. A request's session is created in a dependency, on one worker
    thread, and first used by the handler on another; a transaction begun in between holds a pooled
    (or PgBouncer) server connection idle while the request waits for a thread.
    """
    params = _scope_params(tenant_id, principal)
    session.info[SCOPE] = params
    if read_only:
        session.info[READ_ONLY] = True
    if session.in_transaction():
        session.execute(_SCOPE_SQL, params)


@event.listens_for(Session, "after_begin")
def _apply_scope(session: Session, _transaction: Any, connection: Any) -> None:
    """The first statements of every transaction: read-only if asked, then the lab and principal."""
    if session.info.get(READ_ONLY):
        connection.execute(text("SET TRANSACTION READ ONLY"))
    params = session.info.get(SCOPE)
    if params is not None:
        connection.execute(_SCOPE_SQL, params)


@contextmanager
def tenant_session(tenant_id: uuid.UUID | None = None, *, principal: Principal | None = None, url: str | None = None) -> Iterator[Session]:
    """Open a transaction scoped to exactly one tenant."""
    resolved = tenant_id or current_tenant_id_or_none()
    if resolved is None:
        raise NoTenantContext("tenant_session() needs a tenant: pass one, or wrap the call in `tenant_scope(...)`. For the enumerated cross-tenant reads use `system_session()` instead.")

    # A lab's rows live on its shard; with sharding off this is the one database.
    factory = get_sessionmaker(url or sharding.url_for(resolved))
    with tenant_scope(resolved, principal), factory() as session:
        _bind_scope(session, resolved, principal)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


@contextmanager
def system_session(url: str | None = None) -> Iterator[Session]:
    """For the narrow, enumerated cross-tenant paths only."""
    factory = get_sessionmaker(url)
    with factory() as session:
        _bind_scope(session, None, None)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


#: Set for a request whose browser wrote moments ago; its reads go to the primary so it sees its own change.
RECENT_WRITE: contextvars.ContextVar[bool] = contextvars.ContextVar("radreport_recent_write", default=False)
_lag_checked: dict[str, tuple[float, float]] = {}
_LAG_RECHECK_SECONDS = 5.0


def replica_lag_seconds(url: str) -> float:
    """How far behind the replica is; 0 for a server that is not in recovery."""
    with get_engine(url).connect() as conn:
        lag = conn.execute(text("SELECT CASE WHEN pg_is_in_recovery() THEN COALESCE(EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp()), 0) ELSE 0 END")).scalar()
    return float(lag or 0.0)


def replica_url_for_reads() -> str | None:
    """The replica to read from now, or None for the primary."""
    db = get_settings().db
    if not db.replica_url or RECENT_WRITE.get():
        return None
    now = time.monotonic()
    checked = _lag_checked.get(db.replica_url)
    if checked is None or now - checked[0] > _LAG_RECHECK_SECONDS:
        try:
            lag = replica_lag_seconds(db.replica_url)
        except Exception:  # noqa: BLE001 - an unreachable replica means reading from the primary
            lag = float("inf")
        _lag_checked[db.replica_url] = checked = (now, lag)
    return db.replica_url if checked[1] <= db.replica_max_lag_seconds else None


@contextmanager
def read_session(tenant_id: uuid.UUID | None = None, *, principal: Principal | None = None) -> Iterator[Session]:
    """A read-only transaction, on the replica when replica_url_for_reads allows, bound to one lab or to none."""
    url = replica_url_for_reads() if not sharding.shard_map().enabled else None
    if url is not None:
        instrumentation.mark_replica(url)
    factory = get_sessionmaker(url or sharding.url_for(tenant_id))
    scope = tenant_scope(tenant_id, principal) if tenant_id else nullcontext()
    with scope, factory() as session:
        # First statement of the transaction, as Postgres requires; on the primary it guards against a write slipping in.
        _bind_scope(session, tenant_id, principal, read_only=True)
        session.info["read_only"] = True
        # Closing ends the read-only transaction without expiring what was loaded, as a rollback would.
        yield session


@contextmanager
def read_session_on(url: str) -> Iterator[Session]:
    """A read-only, lab-less transaction on one named database; what a fan-out across shards runs in."""
    with get_sessionmaker(url)() as session:
        _bind_scope(session, None, None, read_only=True)
        yield session


def bind_tenant(session: Session, tenant_id: uuid.UUID, *, principal: Principal | None = None) -> None:
    """Narrow a tenantless session onto one tenant, mid-transaction."""
    current = current_tenant_id_or_none()
    if current is not None and current != tenant_id:
        raise CrossTenantAccess(f"cannot rebind a session scoped to {current} onto {tenant_id}; selecting another tenant replaces scope at the top level, it does not switch mid-transaction")
    _bind_scope(session, tenant_id, principal)


def select_org(session: Session, *, tenant_id: uuid.UUID, platform_user_id: uuid.UUID, ip_address: str | None = None) -> None:
    """A product admin selects an org to configure."""
    from radreport.db.models.orchestration import AuditLog  # local: avoids a cycle

    _bind_scope(session, tenant_id, Principal(id=platform_user_id, kind="platform_user", tenant_id=tenant_id))
    session.add(AuditLog(actor_id=platform_user_id, actor_type="user", action="admin_org_selected", entity_type="tenant", entity_id=tenant_id, tenant_id=tenant_id, after={"selected_tenant_id": str(tenant_id)}, ip_address=ip_address))
    session.flush()
