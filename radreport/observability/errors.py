"""Error reporting to Sentry, on only when a DSN is configured, and stripped of anything that could hold patient data.

Order: setup_error_reporting runs once per process and initialises the SDK with request bodies,
cookies and local variables switched off -> every event passes through _scrub, which drops the
request's body, cookies, query string and auth headers, every exception message and the breadcrumbs
before it leaves the machine.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from radreport.core.config import get_settings
from radreport.core.logging import get_logger

log = get_logger(__name__)

_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "x-platform-user-id", "x-user-id", "x-tenant-id"})


def _scrub(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Drop every part of the request a dictation, a transcript or a credential could be in."""
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "cookies", "query_string", "env"):
            request.pop(key, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}
    event.pop("user", None)
    # An exception's message can quote the transcript; its type and stack trace are enough to group and fix it.
    for exception in (event.get("exception") or {}).get("values") or []:
        if isinstance(exception, dict) and exception.get("value"):
            exception["value"] = "<redacted>"
    event.pop("breadcrumbs", None)
    return event


def setup_error_reporting() -> bool:
    """Start sending unhandled errors to Sentry when a DSN is set; True when it is on."""
    settings = get_settings()
    dsn = settings.observability.sentry_dsn
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        log.warning("error_reporting_unavailable", reason="install the observability extra: pip install -e '.[observability]'")
        return False
    try:
        release = f"radreport@{version('radreport')}"
    except PackageNotFoundError:
        release = None
    sentry_sdk.init(dsn=dsn, environment=settings.environment, release=release, send_default_pii=False, include_local_variables=False, max_request_body_size="never", traces_sample_rate=0.0, before_send=_scrub)
    log.info("error_reporting_enabled", environment=settings.environment)
    return True
