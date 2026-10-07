"""Health and readiness for load balancers and operators, identifying which instance answered.

Order: name this process once (instance_id) -> stamp every response with it (InstanceIdMiddleware)
-> liveness with per-check detail (health), which never fails the probe on a dependency blip ->
readiness (ready), the 503 gate a load balancer routes on. Other modules add checks
(register_check), e.g. the shared cache.
"""

from __future__ import annotations

import inspect
import os
import platform
import resource
import socket
import time
import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from radreport.core.config import get_settings

router = APIRouter(tags=["ops"])

_STARTED = time.monotonic()
#: name -> a check returning `{"ok": bool, ...detail}`; it must be read-only and quick.
_CHECKS: dict[str, Callable[[], dict[str, Any]]] = {}


@lru_cache(maxsize=1)
def instance_id() -> str:
    """INSTANCE_ID if the platform sets one, else a name made once per process."""
    return os.environ.get("INSTANCE_ID") or os.environ.get("RADREPORT_INSTANCE_ID") or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def register_check(name: str, check: Callable[[], dict[str, Any]]) -> None:
    """Add a dependency to /health and /ready."""
    _CHECKS[name] = check


async def _database() -> dict[str, Any]:
    from radreport.db.async_session import get_async_engine

    started = time.perf_counter()
    # A plain connection and SELECT 1 on the async driver: no session, no tenant binding, nothing written, no thread held.
    async with get_async_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}


def _pool() -> dict[str, Any]:
    from radreport.db.session import get_engine

    pool = get_engine().pool
    size, checked_out = pool.size(), pool.checkedout()  # type: ignore[attr-defined]
    limit = size + get_settings().db.max_overflow
    return {"ok": checked_out < limit, "size": size, "checked_out": checked_out, "overflow": pool.overflow(), "limit": limit}  # type: ignore[attr-defined]


def _memory() -> dict[str, Any]:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    mb = rss / (1024 * 1024) if platform.system() == "Darwin" else rss / 1024
    return {"ok": True, "max_rss_mb": round(mb, 1)}


async def _run_checks() -> tuple[bool, dict[str, Any]]:
    results: dict[str, Any] = {}
    healthy = True
    for name, check in {"database": _database, "pool": _pool, "memory": _memory, **_CHECKS}.items():
        try:
            outcome = await check() if inspect.iscoroutinefunction(check) else check()
        except Exception as exc:  # noqa: BLE001 - a failing check is reported, never raised
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        results[name] = outcome
        healthy = healthy and bool(outcome.get("ok"))
    return healthy, results


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness: always 200 while the process serves, with the state of each dependency for operators."""
    healthy, checks = await _run_checks()
    settings = get_settings()
    return {"status": "healthy" if healthy else "degraded", "instance_id": instance_id(), "environment": settings.environment, "version": "0.1.0", "uptime_seconds": round(time.monotonic() - _STARTED, 1), "checks": checks}


class InstanceIdMiddleware:
    """Adds X-Instance-Id to every response, so a request can be traced to the instance behind the load balancer."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.header = (b"x-instance-id", instance_id().encode())

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), self.header]}
            await send(message)

        await self.app(scope, receive, send_with_id)


def readiness_checks() -> tuple[bool, dict[str, str]]:
    """Registered dependencies, for /ready."""
    ok, results = True, {}
    for name, check in _CHECKS.items():
        try:
            outcome = check()
        except Exception as exc:  # noqa: BLE001
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        results[name] = "ok" if outcome.get("ok") else str(outcome.get("error", "failed"))
        ok = ok and bool(outcome.get("ok"))
    return ok, results
