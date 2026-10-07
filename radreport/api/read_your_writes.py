"""Keeps a browser on the primary for a few seconds after it writes, so a replica that lags never hides its own change.

Order: a request carrying the marker cookie, or X-Read-Primary from an API client, reads from the
primary (RECENT_WRITE) -> a successful write sets the marker on its response. Only active when a
replica is configured.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any

from radreport.core.config import get_settings
from radreport.db.session import RECENT_WRITE

COOKIE = "radreport_wrote"


class ReadYourWritesMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        settings = get_settings().db
        if scope["type"] != "http" or not settings.replica_url:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        cookies = SimpleCookie(headers.get(b"cookie", b"").decode("latin-1"))
        recent = COOKIE in cookies or headers.get(b"x-read-primary") == b"1"
        writing = scope["method"] not in ("GET", "HEAD", "OPTIONS")

        async def send_marked(message: dict[str, Any]) -> None:
            if writing and message["type"] == "http.response.start" and message["status"] < 400:
                secure = "; Secure" if get_settings().environment not in ("local", "test", "development") else ""
                cookie = f"{COOKIE}=1; Max-Age={settings.read_your_writes_seconds}; Path=/; HttpOnly; SameSite=Lax{secure}"
                message = {**message, "headers": [*message.get("headers", []), (b"set-cookie", cookie.encode())]}
            await send(message)

        token = RECENT_WRITE.set(recent or writing)
        try:
            await self.app(scope, receive, send_marked)
        finally:
            RECENT_WRITE.reset(token)
