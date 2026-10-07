"""Opens database connections and binds each one to a single lab, so a query cannot reach another lab's rows.

Order: get the engine and session factory (get_engine, get_sessionmaker) -> open a unit of work
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

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from radreport.core.config import get_settings
from radreport.core.errors import CrossTenantAccess, NoTenantContext
from radreport.core.tenancy import PRINCIPAL_GUC, TENANT_GUC, Principal, current_tenant_id_or_none, tenant_scope
from radreport.db import instrumentation, sharding

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


@lru_cache(maxsize=16)
def get_engine(url: str | None = None) -> Engine:
    instrumentation.install()
    sharding.install_tenant_sync()
    # Tenant scope lives in `SET LOCAL`, so a connection handed back to the pool carries nothing.
    return create_engine(url or get_settings().database_url, **engine_options())


def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


def _bind_scope(session: Session, tenant_id: uuid.UUID | None, principal: Principal | None) -> None:
    """Apply the session GUCs the RLS policies read."""
    # Both in one round trip: every session opens with this.
    session.execute(text(f"SELECT set_config('{TENANT_GUC}', :tid, true), set_config('{PRINCIPAL_GUC}', :kind, true)"), {"tid": str(tenant_id) if tenant_id else "", "kind": principal.kind if principal else "system"})


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
        session.execute(text("SET TRANSACTION READ ONLY"))
        _bind_scope(session, tenant_id, principal)
        session.info["read_only"] = True
        try:
            yield session
        finally:
            session.rollback()


@contextmanager
def read_session_on(url: str) -> Iterator[Session]:
    """A read-only, lab-less transaction on one named database; what a fan-out across shards runs in."""
    with get_sessionmaker(url)() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        _bind_scope(session, None, None)
        try:
            yield session
        finally:
            session.rollback()


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
