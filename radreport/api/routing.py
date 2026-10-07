"""The route class every router uses: sync endpoints run bridged, on the event loop with the async driver.

Order: a router built with route_class=BridgedRoute wraps each sync endpoint not marked
`threaded` (db/bridge.py) in an async function that runs it in a SQLAlchemy greenlet -> its
dependencies see the wrapper (request.scope["endpoint"]) and open bridged sessions too (api/deps.py).
Endpoints that are already async are left as they are.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from fastapi.routing import APIRoute

from radreport.db.bridge import THREADED, bridged


class BridgedRoute(APIRoute):
    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if not inspect.iscoroutinefunction(endpoint) and not getattr(endpoint, THREADED, False):
            endpoint = bridged(endpoint)
        super().__init__(path, endpoint, **kwargs)
