"""Prometheus metrics for the web app and the workers, with no lab or patient detail in any label.

Order: each process records into the metrics below as it works (HttpMetricsMiddleware for every
request, observe_stage for each pipeline stage, observe_job for each background job, CACHE_REQUESTS
and RATE_LIMITED from their own modules) -> a scrape adds the job-queue and outbox backlog, counted
in the database at that moment (BacklogCollector) -> render merges every worker process's numbers
when PROMETHEUS_MULTIPROC_DIR is set (process_registry), so a scrape that lands on one worker
still reports them all.

Labels are bounded on purpose: a route is its access-policy id, never the raw path (which carries
ids), and nothing is labelled by lab, since a label per lab grows without limit and names customers.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from radreport.core.logging import get_logger

log = get_logger(__name__)

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_STAGE_BUCKETS = (0.001, 0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

HTTP_REQUESTS = Counter("radreport_http_requests_total", "HTTP requests answered, by access-policy route, method and status class.", ["route", "method", "status"])
HTTP_LATENCY = Histogram("radreport_http_request_duration_seconds", "Time from request received to response finished, by access-policy route.", ["route", "method"], buckets=_LATENCY_BUCKETS)
STAGE_DURATION = Histogram("radreport_pipeline_stage_duration_seconds", "Time each pipeline stage took, by stage and outcome.", ["stage", "status"], buckets=_STAGE_BUCKETS)
LLM_TOKENS = Counter("radreport_llm_tokens_total", "Model tokens used by pipeline stages, by stage and kind (input, output, cache_read, cache_write).", ["stage", "kind"])
LLM_COST = Counter("radreport_llm_cost_usd_total", "Model spend in US dollars, by pipeline stage.", ["stage"])
JOBS = Counter("radreport_jobs_total", "Background jobs finished, by kind and outcome (succeeded, failed).", ["kind", "outcome"])
JOB_DURATION = Histogram("radreport_job_duration_seconds", "Time a background job took, by kind.", ["kind"], buckets=_STAGE_BUCKETS)
CACHE_REQUESTS = Counter("radreport_cache_requests_total", "Cache lookups, by cache (request, shared) and result (hit, miss).", ["cache", "result"])
RATE_LIMITED = Counter("radreport_rate_limited_total", "Requests refused with 429, by rate-limit id.", ["limit"])


def _status_class(code: int) -> str:
    return f"{code // 100}xx"


class HttpMetricsMiddleware:
    """Counts and times every request under its access-policy route id; a plain ASGI middleware, so streaming responses are timed to their end."""

    def __init__(self, app: Callable[..., Any], *, route_of: Callable[[str, str], str]) -> None:
        self.app = app
        self.route_of = route_of

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        route = self.route_of(method, scope["path"])
        started = time.perf_counter()
        status = 500

        async def _send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            HTTP_REQUESTS.labels(route, method, _status_class(status)).inc()
            HTTP_LATENCY.labels(route, method).observe(time.perf_counter() - started)


def observe_stage(stage: str, status: str, duration_ms: float | None, *, tokens_in: int | None = None, tokens_out: int | None = None, cache_read_tokens: int | None = None, cache_write_tokens: int | None = None, cost_usd: float | None = None) -> None:
    """Record one pipeline stage's time, tokens and spend."""
    STAGE_DURATION.labels(stage, status).observe((duration_ms or 0) / 1000)
    for kind, count in (("input", tokens_in), ("output", tokens_out), ("cache_read", cache_read_tokens), ("cache_write", cache_write_tokens)):
        if count:
            LLM_TOKENS.labels(stage, kind).inc(count)
    if cost_usd:
        LLM_COST.labels(stage).inc(float(cost_usd))


def observe_job(kind: str, outcome: str, seconds: float) -> None:
    """Record one finished background job."""
    JOBS.labels(kind, outcome).inc()
    JOB_DURATION.labels(kind).observe(seconds)


class BacklogCollector(Collector):
    """Job-queue and outbox depth, counted in the database at scrape time; a failed count is left out, never reported as zero."""

    def __init__(self, count: Callable[[], list[tuple[str, str, str, int, float]]] | None = None) -> None:
        self._count = count or _count_backlog

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            rows = self._count()
        except Exception as exc:  # noqa: BLE001 - a scrape must answer even with the database down
            log.warning("backlog_metrics_unavailable", error=type(exc).__name__)
            return
        jobs = GaugeMetricFamily("radreport_jobs_backlog", "Background jobs waiting, running or dead, by kind and state.", labels=["kind", "state"])
        job_age = GaugeMetricFamily("radreport_jobs_oldest_seconds", "Age of the oldest job in each kind and state.", labels=["kind", "state"])
        outbox = GaugeMetricFamily("radreport_outbox_unsent", "Domain events committed but not yet published, by topic.", labels=["topic"])
        outbox_age = GaugeMetricFamily("radreport_outbox_oldest_unsent_seconds", "Age of the oldest unpublished event, by topic.", labels=["topic"])
        for source, kind, state, items, oldest in rows:
            if source == "job":
                jobs.add_metric([kind, state], items)
                job_age.add_metric([kind, state], oldest)
            else:
                outbox.add_metric([kind], items)
                outbox_age.add_metric([kind], oldest)
        yield from (jobs, job_age, outbox, outbox_age)

    def describe(self) -> list[GaugeMetricFamily]:
        return []


def _count_backlog() -> list[tuple[str, str, str, int, float]]:
    from sqlalchemy import text

    from radreport.db.session import side_session

    with side_session() as session:
        session.execute(text("SET LOCAL statement_timeout = '2s'"))
        return [tuple(row) for row in session.execute(text("SELECT source, kind, state, items, oldest_seconds FROM work_backlog()"))]


def process_registry() -> CollectorRegistry:
    """This process's metrics, or every worker process's when PROMETHEUS_MULTIPROC_DIR is set."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return REGISTRY
    from prometheus_client import multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


def render(*, backlog: Collector | None = None) -> tuple[bytes, str]:
    """The text exposition format and its content type: the process metrics, then the live backlog."""
    live = CollectorRegistry()
    live.register(backlog or BacklogCollector())
    return generate_latest(process_registry()) + generate_latest(live), CONTENT_TYPE_LATEST
