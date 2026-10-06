"""Shared request plumbing: who the access check let through, and a database session bound to their lab.

Order: read the caller the access middleware identified (current_admin, current_principal,
client_ip) -> open a session bound to one lab (get_db for a lab user, admin_lab_session and
get_admin_lab_db for an admin acting on a lab).
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from radreport.admin.auth import AuthenticatedAdmin
from radreport.api.access import Identity
from radreport.cache.lookups import tenant_config
from radreport.core.tenancy import Principal, tenant_scope
from radreport.db.session import ACTING_PLATFORM_USER, get_sessionmaker, select_org, tenant_session


def _identity(request: Request) -> Identity | None:
    return getattr(request.state, "identity", None)


def client_ip(request: Request) -> str | None:
    """The caller's address if it is a real IP; the audit column is `inet` and refuses anything else."""
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def current_admin(request: Request) -> AuthenticatedAdmin:
    """The signed-in admin the access middleware resolved."""
    identity = _identity(request)
    if identity is None or identity.admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in at /admin/login")
    return identity.admin


CurrentAdmin = Annotated[AuthenticatedAdmin, Depends(current_admin)]


def current_principal(request: Request) -> Principal:
    """The lab user the access middleware resolved."""
    identity = _identity(request)
    if identity is None or identity.principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing principal headers")
    return identity.principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def get_db(principal: CurrentPrincipal) -> Iterator[Session]:
    """A session bound to the lab user's own tenant."""
    assert principal.tenant_id is not None
    with tenant_session(principal.tenant_id, principal=principal) as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]


@contextmanager
def admin_lab_session(admin: AuthenticatedAdmin, tenant_id: uuid.UUID, *, ip_address: str | None = None) -> Iterator[Session]:
    """A session an admin uses on one lab: bound to it for RLS, with the selection audited."""
    principal = Principal(id=admin.platform_user_id, kind="platform_user", tenant_id=tenant_id)
    with tenant_scope(tenant_id, principal), get_sessionmaker()() as session:
        if tenant_config(session, tenant_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no lab {tenant_id}")
        select_org(session, tenant_id=tenant_id, platform_user_id=admin.platform_user_id, ip_address=ip_address)
        # On the session, not only in a context variable: FastAPI runs this dependency in another thread.
        session.info[ACTING_PLATFORM_USER] = admin.platform_user_id
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_admin_lab_db(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin) -> Iterator[Session]:
    """`admin_lab_session` for the `{tenant_id}` in the route's path."""
    with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
        yield session


AdminLabDb = Annotated[Session, Depends(get_admin_lab_db)]
