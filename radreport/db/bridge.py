"""Runs sync request code on the event loop, with every query on the async driver.

Order: a sync route not marked `threaded` is called inside a SQLAlchemy greenlet (bridged, run)
-> inside it the session factories hand out sessions on the async engine (in_bridge, which
db/session.py checks), so each query awaits psycopg's asyncio driver and the code holds no worker
thread -> work that would block the loop and touches no session goes to a thread (offload).

This is the mechanism AsyncSession itself is built on: the ORM code stays synchronous, and the
driver's waits become awaits on the request's own event loop. Routes that spend seconds of CPU
between queries (onboarding uploads and mining steps) are marked `threaded` and keep the sync
engine on a worker thread, so they cannot stall every other request in the process.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import anyio.to_thread
from sqlalchemy.util.concurrency import await_only, greenlet_spawn, in_greenlet

#: Attribute set on a route function that must stay on a worker thread with the sync engine.
THREADED = "__radreport_threaded__"
#: Attribute set on the async wrapper that runs a sync route bridged.
BRIDGED = "__radreport_bridged__"


def in_bridge() -> bool:
    """True inside bridged code: sessions opened here use the async engine."""
    return in_greenlet()


def threaded[T](fn: Callable[..., T]) -> Callable[..., T]:
    """Mark a sync route to run on a worker thread with the sync engine, as before the bridge."""
    setattr(fn, THREADED, True)
    return fn


async def run[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call sync code bridged, from async code."""
    return await greenlet_spawn(fn, *args, **kwargs)


def bridged[T](fn: Callable[..., T]) -> Callable[..., Any]:
    """An async wrapper that runs the sync route `fn` bridged; FastAPI reads `fn`'s signature through it."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await greenlet_spawn(fn, *args, **kwargs)

    setattr(wrapper, BRIDGED, True)
    return wrapper


def is_bridged(endpoint: Any) -> bool:
    """Whether a route's endpoint runs bridged, so its dependencies should open bridged sessions."""
    return bool(getattr(endpoint, BRIDGED, False))


def offload[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run blocking work that touches no session on a thread when bridged; call it directly otherwise."""
    if not in_greenlet():
        return fn(*args, **kwargs)
    return await_only(anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs)))
