"""A cache that lives for one HTTP request, so a lookup repeated inside it reaches the database once.

Order: open a scope per request (request_scope, RequestCacheMiddleware) -> look a value up
(request_cached), which loads it on the first call and serves the copy afterwards -> read the
hit rate (STATS).
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from radreport.cache.keys import CacheKey

_MISSING = object()
_scope: contextvars.ContextVar[dict[CacheKey, Any] | None] = contextvars.ContextVar("radreport_request_cache", default=None)


@dataclass
class HitStats:
    """Hits and misses, for the hit-rate check."""

    hits: int = 0
    misses: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def hit(self) -> None:
        with self._lock:
            self.hits += 1

    def miss(self) -> None:
        with self._lock:
            self.misses += 1

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict[str, float]:
        return {"hits": self.hits, "misses": self.misses, "hit_rate": round(self.hit_rate, 4)}

    def reset(self) -> None:
        with self._lock:
            self.hits = self.misses = 0


STATS = HitStats()


@contextmanager
def request_scope() -> Iterator[dict[CacheKey, Any]]:
    """Values cached inside the block are dropped when it ends."""
    store: dict[CacheKey, Any] = {}
    token = _scope.set(store)
    try:
        yield store
    finally:
        _scope.reset(token)


def request_cached[T](cache_key: CacheKey, loader: Callable[[], T]) -> T:
    """The value for `cache_key` in this request, loading it once. Outside a request, always loads."""
    store = _scope.get()
    if store is None:
        return loader()
    value = store.get(cache_key, _MISSING)
    if value is not _MISSING:
        STATS.hit()
        return value  # type: ignore[no-any-return]
    STATS.miss()
    value = loader()
    store[cache_key] = value
    return value


def forget(cache_key: CacheKey) -> None:
    """Drop one value from this request's cache, after the request itself changed it."""
    store = _scope.get()
    if store is not None:
        store.pop(cache_key, None)


class RequestCacheMiddleware:
    """Opens a request-cache scope around each HTTP request."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with request_scope():
            await self.app(scope, receive, send)
