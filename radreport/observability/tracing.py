"""OpenTelemetry tracing, on only when an OTLP endpoint is configured, and a span helper that costs nothing when it is off.

Order: setup_tracing runs once per process (the app factory, a worker's start) and, when
OTEL_EXPORTER_OTLP_ENDPOINT is set and the packages are installed, exports spans for every request,
SQL statement (_install_sql_spans) and outgoing HTTP call -> code marks its own units of work with span (one per
pipeline stage). SQL spans carry the statement with placeholders, never its parameters.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from radreport.core.config import get_settings
from radreport.core.logging import get_logger

log = get_logger(__name__)

#: Probes and scrapes would bury real traffic in traces.
_UNTRACED = "health,ready,metrics,ui/static"

_enabled = False


def setup_tracing(app: Any | None = None) -> bool:
    """Start exporting spans when OTEL_EXPORTER_OTLP_ENDPOINT is set; True when tracing is on."""
    global _enabled
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("tracing_unavailable", reason="install the observability extra: pip install -e '.[observability]'")
        return False

    if not _enabled:
        settings = get_settings()
        provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", settings.observability.service_name), "deployment.environment": settings.environment}))
        # The exporter reads its endpoint and auth headers from the standard OTEL_EXPORTER_OTLP_* variables.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _install_sql_spans()
        HTTPXClientInstrumentor().instrument()
        _enabled = True
        log.info("tracing_enabled", endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls=_UNTRACED)
    return True


def _install_sql_spans() -> None:
    """One span per SQL statement, from class-level engine events, so every engine is covered however and whenever it was created."""
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    tracer = trace.get_tracer("radreport.sql")

    @event.listens_for(Engine, "before_cursor_execute")
    def _start(_conn: Any, _cursor: Any, statement: str, _params: Any, context: Any, _many: bool) -> None:
        verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "SQL"
        # The statement with its placeholders only: parameters can hold patient data.
        context._otel_span = tracer.start_span(f"db {verb}", attributes={"db.system": "postgresql", "db.statement": statement[:2000]})

    @event.listens_for(Engine, "after_cursor_execute")
    def _end(_conn: Any, _cursor: Any, _statement: str, _params: Any, context: Any, _many: bool) -> None:
        if (current := getattr(context, "_otel_span", None)) is not None:
            current.end()
            context._otel_span = None

    @event.listens_for(Engine, "handle_error")
    def _failed(exception_context: Any) -> None:
        context = exception_context.execution_context
        if context is not None and (current := getattr(context, "_otel_span", None)) is not None:
            current.set_status(Status(StatusCode.ERROR, type(exception_context.original_exception).__name__))
            current.end()
            context._otel_span = None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """A child span around a unit of work; a no-op when tracing is off."""
    if not _enabled:
        yield
        return
    from opentelemetry import trace

    with trace.get_tracer("radreport").start_as_current_span(name, attributes={f"radreport.{k}": v for k, v in attributes.items() if v is not None}):
        yield
