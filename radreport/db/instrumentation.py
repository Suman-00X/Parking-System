"""Counts and times every SQL statement, per request and for the whole process, and logs the slow ones.

Order: install the engine listeners once (install) -> open a scope per request or job
(query_scope, current_stats) -> each statement is timed (_before, _after) and a slow one is
logged without its parameters -> read the process-wide picture (METRICS.snapshot).
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

from radreport.core.config import get_settings
from radreport.core.logging import get_logger

log = get_logger(__name__)

#: The statement text kept in a slow-query log line; parameters are never logged, since they can hold patient data.
_STATEMENT_CHARS = 400
_START_KEY = "radreport_query_started"


@dataclass(slots=True)
class QueryStats:
    """The statements one request or job ran."""

    label: str = "unscoped"
    count: int = 0
    total_ms: float = 0.0
    slow: int = 0
    statements: list[str] | None = None
    """Kept only when a test asks to see the statements, never in production."""

    def record(self, ms: float, statement: str, *, slow: bool) -> None:
        self.count += 1
        self.total_ms += ms
        self.slow += int(slow)
        if self.statements is not None:
            self.statements.append(statement)


_current: contextvars.ContextVar[QueryStats | None] = contextvars.ContextVar("radreport_query_stats", default=None)


def current_stats() -> QueryStats | None:
    """The scope the running code belongs to, if any."""
    return _current.get()


@contextmanager
def query_scope(label: str, *, keep_statements: bool = False) -> Iterator[QueryStats]:
    """Count the statements run inside the block, including those run on worker threads it starts."""
    stats = QueryStats(label=label, statements=[] if keep_statements else None)
    token = _current.set(stats)
    try:
        yield stats
    finally:
        _current.reset(token)
        METRICS.record_scope(stats)


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass(slots=True)
class _RouteTotals:
    scopes: int = 0
    queries: int = 0
    ms: float = 0.0
    max_queries: int = 0


@dataclass
class QueryMetrics:
    """Process-wide statement timings, kept in a bounded window so memory stays flat."""

    window: int = 10_000
    _durations: deque[float] = field(default_factory=deque)
    _per_scope: deque[int] = field(default_factory=deque)
    _routes: dict[str, _RouteTotals] = field(default_factory=dict)
    _total: int = 0
    _slow: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_query(self, ms: float, *, slow: bool) -> None:
        with self._lock:
            self._durations.append(ms)
            if len(self._durations) > self.window:
                self._durations.popleft()
            self._total += 1
            self._slow += int(slow)

    def record_scope(self, stats: QueryStats) -> None:
        with self._lock:
            self._per_scope.append(stats.count)
            if len(self._per_scope) > self.window:
                self._per_scope.popleft()
            totals = self._routes.setdefault(stats.label, _RouteTotals())
            totals.scopes += 1
            totals.queries += stats.count
            totals.ms += stats.total_ms
            totals.max_queries = max(totals.max_queries, stats.count)

    def snapshot(self, *, top: int = 20) -> dict[str, Any]:
        """Percentiles of statement time and of statements per request, plus the heaviest routes."""
        with self._lock:
            durations = sorted(self._durations)
            per_scope = sorted(self._per_scope)
            routes = sorted(self._routes.items(), key=lambda kv: kv[1].queries / max(kv[1].scopes, 1), reverse=True)[:top]
            total, slow = self._total, self._slow
        return {
            "queries_total": total,
            "slow_queries_total": slow,
            "slow_query_ms": get_settings().db.slow_query_ms,
            "query_ms": {"p50": round(_percentile(durations, 0.50), 3), "p95": round(_percentile(durations, 0.95), 3), "p99": round(_percentile(durations, 0.99), 3), "sampled": len(durations)},
            "queries_per_request": {"p50": _percentile([float(c) for c in per_scope], 0.50), "p95": _percentile([float(c) for c in per_scope], 0.95), "p99": _percentile([float(c) for c in per_scope], 0.99), "sampled": len(per_scope)},
            "routes": [{"route": name, "requests": t.scopes, "avg_queries": round(t.queries / max(t.scopes, 1), 2), "max_queries": t.max_queries, "avg_db_ms": round(t.ms / max(t.scopes, 1), 3)} for name, t in routes],
        }

    def reset(self) -> None:
        with self._lock:
            self._durations.clear()
            self._per_scope.clear()
            self._routes.clear()
            self._total = self._slow = 0


METRICS = QueryMetrics()


def _before(conn: Any, _cursor: Any, _statement: str, _parameters: Any, context: Any, _executemany: bool) -> None:
    conn.info.setdefault(_START_KEY, []).append(time.perf_counter())


def _after(conn: Any, _cursor: Any, statement: str, _parameters: Any, context: Any, _executemany: bool) -> None:
    starts = conn.info.get(_START_KEY)
    if not starts:
        return
    ms = (time.perf_counter() - starts.pop()) * 1000.0
    threshold = get_settings().db.slow_query_ms
    slow = threshold > 0 and ms >= threshold
    stats = _current.get()
    if stats is not None:
        stats.record(ms, statement, slow=slow)
    METRICS.record_query(ms, slow=slow)
    if slow:
        log.warning("slow_query", ms=round(ms, 1), threshold_ms=threshold, scope=stats.label if stats else "unscoped", statement=" ".join(statement.split())[:_STATEMENT_CHARS])


_installed = False
_install_lock = threading.Lock()


def install() -> None:
    """Attach the timing listeners to every engine, once per process."""
    global _installed
    with _install_lock:
        if _installed:
            return
        event.listen(Engine, "before_cursor_execute", _before)
        event.listen(Engine, "after_cursor_execute", _after)
        _installed = True


class QueryMetricsMiddleware:
    """Opens a query scope per HTTP request and reports its count and time."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Labelled by the access-policy route id once it is known, so paths full of ids do not each get a bucket.
        label = "unmatched"
        expose = get_settings().environment in ("local", "test", "development")
        with query_scope(label) as stats:
            started = time.perf_counter()

            async def send_with_timing(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start" and expose:
                    # Server-Timing shows up in the browser's network panel; only where the internals are not sensitive.
                    headers = list(message.get("headers", []))
                    headers.append((b"server-timing", f'db;dur={stats.total_ms:.1f};desc="{stats.count} queries"'.encode()))
                    headers.append((b"x-query-count", str(stats.count).encode()))
                    message = {**message, "headers": headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_timing)
            finally:
                rule = scope.get("state", {}).get("access_rule")
                if rule is not None:
                    stats.label = rule.id
                log.debug("request_queries", route=stats.label, queries=stats.count, db_ms=round(stats.total_ms, 1), wall_ms=round((time.perf_counter() - started) * 1000.0, 1))
