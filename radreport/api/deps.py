"""Shared request plumbing: who the access check let through, and a database session bound to their lab.

Order: read the caller the access middleware identified (current_admin, current_principal,
client_ip) -> open a session bound to one lab (get_db for a lab user, get_read_db for one that only
reads and may use the replica, admin_lab_session and get_admin_lab_db for an admin acting on a lab).
A bridged route's session is opened and closed in greenlets on the async engine; a threaded
route's on worker threads with the sync engine (db/bridge.py).
"""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Annotated

import anyio
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from radreport.admin.auth import AuthenticatedAdmin
from radreport.api.access import Identity
from radreport.cache.lookups import tenant_config
from radreport.core.config import get_settings
from radreport.core.tenancy import Principal, tenant_scope
from radreport.db import bridge, sharding
from radreport.db.session import ACTING_PLATFORM_USER, get_sessionmaker, read_session, select_org, tenant_session


def _identity(request: Request) -> Identity | None:
    return getattr(request.state, "identity", None)


def client_ip(request: Request) -> str | None:
    """The caller's address if it is a real IP; the audit column is `inet` and refuses anything else."""
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


async def current_admin(request: Request) -> AuthenticatedAdmin:
    """The signed-in admin the access middleware resolved."""
    identity = _identity(request)
    if identity is None or identity.admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in at /admin/login")
    return identity.admin


CurrentAdmin = Annotated[AuthenticatedAdmin, Depends(current_admin)]


async def current_principal(request: Request) -> Principal:
    """The lab user the access middleware resolved."""
    identity = _identity(request)
    if identity is None or identity.principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing principal headers")
    return identity.principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


#: Teardown threads apart from the request threadpool, so closing a session never waits behind requests that want one.
_TEARDOWN = anyio.CapacityLimiter(1000)
_admission: dict[int, anyio.Semaphore] = {}


def _admission_gate() -> anyio.Semaphore:
    """As many request sessions at once as the pool has connections, per worker.

    A request waits here, on the event loop and holding no thread, until a connection is certain.
    Without it, under overload every thread can end up waiting for a connection while the requests
    that hold the connections wait for a thread to finish on: a deadlock.
    """
    loop = id(asyncio.get_running_loop())
    gate = _admission.get(loop)
    if gate is None:
        db = get_settings().db
        gate = _admission[loop] = anyio.Semaphore(db.pool_size + db.max_overflow)
    return gate


async def _held(cm: AbstractContextManager[Session], *, enter_in_thread: bool, bridge: bool = False) -> AsyncIterator[Session]:
    """Yield a session and always close it, even when the request is cancelled.

    FastAPI's own wrapper for a sync generator dependency skips the teardown on cancellation (a
    client that gives up mid-request), which leaks the session's connection; under overload those
    leaks stall every worker. Here the commit or rollback runs shielded from cancellation.
    """
    gate = _admission_gate()
    await gate.acquire()
    try:
        opened = _opened_bridged(cm) if bridge else _opened(cm, enter_in_thread=enter_in_thread)
        async for session in opened:
            yield session
    finally:
        gate.release()


async def _opened_bridged(cm: AbstractContextManager[Session]) -> AsyncIterator[Session]:
    """Open and close the session in greenlets on the event loop, so it lives on the async engine like the route using it."""
    session = await bridge.run(cm.__enter__)
    try:
        yield session
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            suppressed = await bridge.run(cm.__exit__, type(exc), exc, exc.__traceback__)
        if not suppressed:
            raise
    else:
        with anyio.CancelScope(shield=True):
            await bridge.run(cm.__exit__, None, None, None)


def _bridged(request: Request) -> bool:
    """Whether the matched route runs bridged, so its session must be opened on the async engine."""
    return bridge.is_bridged(request.scope.get("endpoint"))


async def _opened(cm: AbstractContextManager[Session], *, enter_in_thread: bool) -> AsyncIterator[Session]:
    session = await anyio.to_thread.run_sync(cm.__enter__) if enter_in_thread else cm.__enter__()
    try:
        yield session
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            suppressed = await anyio.to_thread.run_sync(cm.__exit__, type(exc), exc, exc.__traceback__, limiter=_TEARDOWN)
        if not suppressed:
            raise
    else:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(cm.__exit__, None, None, None, limiter=_TEARDOWN)


async def get_db(principal: CurrentPrincipal, request: Request) -> AsyncIterator[Session]:
    """A session bound to the lab user's own tenant."""
    assert principal.tenant_id is not None
    # Entered on the event loop: opening it does no I/O (the lab is bound when the first query starts a transaction).
    async for session in _held(tenant_session(principal.tenant_id, principal=principal), enter_in_thread=False, bridge=_bridged(request)):
        yield session


#: scope="function": the session commits and closes when the handler returns, before the response is sent. A client never sees
#: success for a write that has not committed, and a slow or vanished client cannot keep a connection checked out.
DbSession = Annotated[Session, Depends(get_db, scope="function")]


async def get_read_db(principal: CurrentPrincipal, request: Request) -> AsyncIterator[Session]:
    """A read-only session for the lab user's tenant, on the replica when one is usable."""
    assert principal.tenant_id is not None
    async for session in _held(read_session(principal.tenant_id, principal=principal), enter_in_thread=True, bridge=_bridged(request)):
        yield session


ReadDbSession = Annotated[Session, Depends(get_read_db, scope="function")]


@contextmanager
def admin_lab_session(admin: AuthenticatedAdmin, tenant_id: uuid.UUID, *, ip_address: str | None = None) -> Iterator[Session]:
    """A session an admin uses on one lab: bound to it for RLS, with the selection audited."""
    principal = Principal(id=admin.platform_user_id, kind="platform_user", tenant_id=tenant_id)
    with tenant_scope(tenant_id, principal), get_sessionmaker(sharding.url_for(tenant_id))() as session:
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


async def get_admin_lab_db(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin) -> AsyncIterator[Session]:
    """`admin_lab_session` for the `{tenant_id}` in the route's path."""
    async for session in _held(admin_lab_session(admin, tenant_id, ip_address=client_ip(request)), enter_in_thread=True, bridge=_bridged(request)):
        yield session


AdminLabDb = Annotated[Session, Depends(get_admin_lab_db, scope="function")]
