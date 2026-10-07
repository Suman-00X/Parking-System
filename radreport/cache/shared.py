"""A cache shared by every request and, with Redis, by every worker and instance; in memory when Redis is not configured.

Order: pick the backend (get_backend: RedisBackend when RADREPORT_REDIS_URL is set, else
MemoryBackend) -> read through both layers, request first (cached) -> drop a value everywhere after
changing it (invalidate) -> report it to /health (_health). Values are JSON, never pickles: a
cache that can be written to must not be able to run code in the reader.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Protocol

from radreport.cache import request
from radreport.cache.keys import CacheKey
from radreport.cache.request import HitStats
from radreport.core.config import get_settings
from radreport.core.logging import get_logger

log = get_logger(__name__)

STATS = HitStats()
_MISSING = object()


class Backend(Protocol):
    name: str
    shared: bool
    """True when every worker sees the same values, so one worker's invalidation reaches the rest."""

    def get(self, key: str) -> bytes | None: ...
    def set(self, key: str, value: bytes, ttl_seconds: float) -> None: ...
    def delete(self, *keys: str) -> None: ...
    def incr(self, key: str) -> int: ...
    def ping(self) -> bool: ...


class MemoryBackend:
    """Per-process, TTL-bounded and size-bounded. Invalidation reaches only this worker, so TTLs bound how stale others get."""

    name = "memory"
    shared = False

    def __init__(self, max_entries: int = 10_000, clock: Callable[[], float] = time.monotonic) -> None:
        self._data: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires, value = hit
            if expires < self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: bytes, ttl_seconds: float) -> None:
        with self._lock:
            self._data[key] = (self._clock() + ttl_seconds, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def delete(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._data.pop(key, None)

    def incr(self, key: str) -> int:
        with self._lock:
            _expires, raw = self._data.get(key, (float("inf"), b"0"))
            value = int(raw) + 1
            self._data[key] = (float("inf"), str(value).encode())
            return value

    def ping(self) -> bool:
        return True

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RedisBackend:
    """Redis, with short timeouts. Any error is a miss: a cache outage must slow the app down, not take it down."""

    name = "redis"
    shared = True

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.25, health_check_interval=30)
        self._client = client

    def _safely(self, op: str, fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - fail open
            log.warning("shared_cache_unavailable", op=op, error=type(exc).__name__)
            return default

    def get(self, key: str) -> bytes | None:
        return self._safely("get", lambda: self._client.get(key))

    def set(self, key: str, value: bytes, ttl_seconds: float) -> None:
        self._safely("set", lambda: self._client.set(key, value, px=max(1, int(ttl_seconds * 1000))))

    def delete(self, *keys: str) -> None:
        if keys:
            self._safely("delete", lambda: self._client.delete(*keys))

    def incr(self, key: str) -> int:
        return int(self._safely("incr", lambda: self._client.incr(key), default=0) or 0)

    def ping(self) -> bool:
        return bool(self._safely("ping", self._client.ping, default=False))


@lru_cache(maxsize=1)
def get_backend() -> Backend:
    url = get_settings().redis_url
    backend: Backend = RedisBackend(url) if url else MemoryBackend()
    log.info("shared_cache_backend", backend=backend.name)
    return backend


def cached[T](cache_key: CacheKey, loader: Callable[[], T], *, ttl_seconds: float, encode: Callable[[T], Any], decode: Callable[[Any], T]) -> T:
    """The value for `cache_key`: from this request, else the shared cache, else `loader` (and then stored in both)."""

    def through_shared() -> T:
        backend = get_backend()
        raw = backend.get(cache_key.render())
        if raw is not None:
            try:
                value = decode(json.loads(raw))
                STATS.hit()
                return value
            except (ValueError, KeyError, TypeError):
                backend.delete(cache_key.render())  # an entry this code version cannot read is just a miss
        STATS.miss()
        value = loader()
        backend.set(cache_key.render(), json.dumps(encode(value)).encode(), ttl_seconds)
        return value

    return request.request_cached(cache_key, through_shared)


def invalidate(*keys: CacheKey) -> None:
    """Drop values from this request and from the shared cache, after the code that read them changed them."""
    for cache_key in keys:
        request.forget(cache_key)
    get_backend().delete(*(k.render() for k in keys))


def invalidate_after_commit(session: Any, *keys: CacheKey) -> None:
    """Invalidate now in this request, and in the shared cache once `session` commits.

    After commit, not before: a reader in between would otherwise load the old row and cache it again for a full TTL.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    for cache_key in keys:
        request.forget(cache_key)
    rendered = [k.render() for k in keys]
    if not isinstance(session, Session):
        get_backend().delete(*rendered)
        return
    event.listen(session, "after_commit", lambda _s: get_backend().delete(*rendered), once=True)


def bump_after_commit(session: Any, counter: str) -> None:
    """Advance a generation counter once `session` commits; entries stamped with the old generation stop being served."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    if not isinstance(session, Session):
        get_backend().incr(counter)
        return
    event.listen(session, "after_commit", lambda _s: get_backend().incr(counter), once=True)


def ttl(*, shared: float, local: float) -> float:
    """A longer TTL when invalidation reaches every worker (Redis), a short one when it reaches only this one."""
    return shared if get_backend().shared else local


def _health() -> dict[str, Any]:
    backend = get_backend()
    started = time.perf_counter()
    ok = backend.ping()
    return {"ok": ok, "backend": backend.name, "shared": backend.shared, "latency_ms": round((time.perf_counter() - started) * 1000, 2), "hit_rate": STATS.as_dict()["hit_rate"]}


def register_health() -> None:
    from radreport.api.routes.health import register_check

    register_check("cache", _health)
