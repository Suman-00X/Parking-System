"""The Prometheus scrape endpoint.

Order: a scrape arrives at /metrics -> it must carry the configured bearer token, or, with none
configured, come to a local, test or development deployment (_authorised) -> the process metrics and
the live backlog are rendered in the text exposition format (metrics).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from radreport.api.routing import BridgedRoute
from radreport.core.config import get_settings
from radreport.observability import metrics as prom

router = APIRouter(tags=["ops"], route_class=BridgedRoute)


def _authorised(request: Request) -> bool:
    """A scraper with the token; with no token configured, only a development deployment."""
    settings = get_settings()
    token = settings.observability.metrics_token
    if not token:
        return settings.environment in ("local", "test", "development")
    sent = request.headers.get("authorization", "")
    return hmac.compare_digest(sent.encode(), f"Bearer {token}".encode())


@router.get("/metrics")
def metrics(request: Request) -> Response:
    """Prometheus metrics: request rates and latency, pipeline stages, model spend, cache, jobs and outbox."""
    if not _authorised(request):
        # Not Found rather than Unauthorized, so the endpoint does not advertise itself.
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    body, content_type = prom.render()
    return Response(body, media_type=content_type)
