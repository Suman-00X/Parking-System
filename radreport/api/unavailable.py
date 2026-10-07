"""Answers 503 with Retry-After when the database cannot be reached, instead of a bare 500.

Order: wrap the whole app, middleware included (DatabaseUnavailableMiddleware) -> a lost or refused
connection, or no free pooled connection in time (is_unavailable), becomes 503 Retry-After -> the
next request gets a fresh connection: pool_pre_ping drops dead ones, so the process recovers on
its own once the database (or PgBouncer) is back, with no restart.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exc as sa_exc
from starlette.responses import JSONResponse

from radreport.core.logging import get_logger

log = get_logger(__name__)

RETRY_AFTER_SECONDS = 5


def is_unavailable(error: BaseException) -> bool:
    """A connection-level failure, as opposed to a bad query: worth retrying in a moment."""
    if isinstance(error, sa_exc.TimeoutError | sa_exc.OperationalError):
        return True
    return isinstance(error, sa_exc.DBAPIError) and bool(error.connection_invalidated)


class DatabaseUnavailableMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as error:
            if started or not is_unavailable(error):
                raise
            log.warning("database_unavailable", path=scope.get("path"), error=type(error.__cause__ or error).__name__)
            await JSONResponse({"detail": "the database is unavailable; retry shortly"}, status_code=503, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})(scope, receive, send)
