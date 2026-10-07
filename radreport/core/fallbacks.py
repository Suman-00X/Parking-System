"""What this process is running without: optional infrastructure that is missing or unusable, and what stands in for it.

Order: a component finds its configuration missing and picks a stand-in (note), logged once per
component -> operators read the list on /health (active, register_health), which never fails
readiness over it: running without Kafka or S3 is a choice, not an outage.
"""

from __future__ import annotations

import threading
from typing import Any

from radreport.core.logging import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_active: dict[str, str] = {}


def note(component: str, detail: str) -> None:
    """Record that `component` runs on a stand-in; logs a warning the first time, or when the reason changes."""
    with _lock:
        if _active.get(component) == detail:
            return
        _active[component] = detail
    log.warning("running_without", component=component, detail=detail)


def active() -> dict[str, str]:
    """Component -> what stands in for it, for every fallback taken so far."""
    with _lock:
        return dict(_active)


def clear() -> None:
    """Forget every fallback (tests)."""
    with _lock:
        _active.clear()


def _health() -> dict[str, Any]:
    return {"ok": True, "active": active()}


def register_health() -> None:
    """List the fallbacks on /health; always ok, so /ready never fails over them."""
    from radreport.api.routes.health import register_check

    register_check("fallbacks", _health)
