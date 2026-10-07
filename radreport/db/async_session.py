"""The async counterpart of db/session.py: the same lab binding, the same pool settings, on an asyncio driver.

Order: get the async engine (get_async_engine; psycopg 3 runs natively on asyncio, and the timing
listeners still see every statement through the engine's sync core) -> open a unit of work for one
lab (async_tenant_session), for the system (async_system_session), or read-only on the replica
when one is usable (async_read_session).

Endpoints move to this one at a time; a route on it runs on the event loop instead of a worker
thread, so a slow query no longer holds one of the threadpool's 40 threads.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from radreport.core.config import get_settings
from radreport.core.errors import NoTenantContext
from radreport.core.tenancy import PRINCIPAL_GUC, TENANT_GUC, Principal, current_tenant_id_or_none, tenant_scope
from radreport.db import instrumentation
from radreport.db.session import READ_ONLY, SCOPE, _scope_params, engine_options, replica_url_for_reads

_engines: dict[tuple[str, int], AsyncEngine] = {}


def get_async_engine(url: str | None = None) -> AsyncEngine:
    """One engine per URL per event loop: an async connection belongs to the loop that opened it."""
    instrumentation.install()
    target = url or get_settings().database_url
    loop = id(asyncio.get_running_loop())
    engine = _engines.get((target, loop))
    if engine is None:
        options = {**engine_options(), "pool_size": get_settings().db.async_pool_size, "max_overflow": get_settings().db.async_max_overflow}
        engine = _engines[(target, loop)] = create_async_engine(target, **options)
    return engine


def _factory(url: str | None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(url), expire_on_commit=False)


async def _bind_scope(session: AsyncSession, tenant_id: uuid.UUID | None, principal: Principal | None, *, read_only: bool = False) -> None:
    """The same deferred binding as the sync sessions: applied by the after_begin listener when the transaction starts."""
    sync = session.sync_session
    sync.info[SCOPE] = _scope_params(tenant_id, principal)
    if read_only:
        sync.info[READ_ONLY] = True
    if sync.in_transaction():
        await session.execute(text(f"SELECT set_config('{TENANT_GUC}', :tid, true), set_config('{PRINCIPAL_GUC}', :kind, true)"), sync.info[SCOPE])


@asynccontextmanager
async def async_tenant_session(tenant_id: uuid.UUID | None = None, *, principal: Principal | None = None, url: str | None = None) -> AsyncIterator[AsyncSession]:
    """A transaction scoped to exactly one lab."""
    resolved = tenant_id or current_tenant_id_or_none()
    if resolved is None:
        raise NoTenantContext("async_tenant_session() needs a tenant")
    with tenant_scope(resolved, principal):
        async with _factory(url)() as session:
            await _bind_scope(session, resolved, principal)
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


@asynccontextmanager
async def async_system_session(url: str | None = None) -> AsyncIterator[AsyncSession]:
    """For the narrow, enumerated cross-lab paths only."""
    async with _factory(url)() as session:
        await _bind_scope(session, None, None)
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def async_read_session(tenant_id: uuid.UUID | None = None, *, principal: Principal | None = None) -> AsyncIterator[AsyncSession]:
    """A read-only transaction, on the replica when read_session would use it."""
    url = replica_url_for_reads()
    if url is not None:
        instrumentation.mark_replica(url)
    async with _factory(url)() as session:
        await _bind_scope(session, tenant_id, principal, read_only=True)
        # Closing (by leaving the block) ends the transaction without expiring what was loaded.
        yield session
