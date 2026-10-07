"""Opens database connections and binds each one to a single lab, so a query cannot reach another lab's rows.

Order: get the engine and session factory (get_engine, get_sessionmaker) -> open a unit of work
for one lab (tenant_session) or for the system (system_session). bind_tenant and select_org are
the lower-level pieces those use.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from radreport.core.config import get_settings
from radreport.core.errors import CrossTenantAccess, NoTenantContext
from radreport.core.tenancy import PRINCIPAL_GUC, TENANT_GUC, Principal, current_tenant_id_or_none, tenant_scope
from radreport.db import instrumentation

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


@lru_cache(maxsize=4)
def get_engine(url: str | None = None) -> Engine:
    instrumentation.install()
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

    factory = get_sessionmaker(url)
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
