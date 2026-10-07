"""The shared cache's backends: TTL and size bounds in memory, and Redis failing open."""

from __future__ import annotations

import uuid

from radreport.cache import shared
from radreport.cache.keys import key
from radreport.cache.request import request_scope


def test_memory_entries_expire_and_the_oldest_are_evicted() -> None:
    now = [0.0]
    backend = shared.MemoryBackend(max_entries=2, clock=lambda: now[0])
    backend.set("a", b"1", 10)
    backend.set("b", b"2", 10)
    backend.get("a")
    backend.set("c", b"3", 10)
    assert backend.get("b") is None, "least recently used goes first"
    assert backend.get("a") == b"1"
    now[0] = 11
    assert backend.get("a") is None, "expired"
    assert backend.incr("g") == 1 and backend.incr("g") == 2


def test_a_redis_outage_is_a_miss_not_an_error() -> None:
    class Broken:
        def __getattr__(self, _name):  # type: ignore[no-untyped-def]
            def fail(*_a, **_k):  # type: ignore[no-untyped-def]
                raise ConnectionError("down")

            return fail

    backend = shared.RedisBackend("redis://unused", client=Broken())
    assert backend.get("k") is None
    backend.set("k", b"v", 1)
    backend.delete("k")
    assert backend.incr("k") == 0 and backend.ping() is False


def test_read_through_loads_once_and_serves_the_shared_copy(monkeypatch) -> None:
    backend = shared.MemoryBackend()
    monkeypatch.setattr(shared, "get_backend", lambda: backend)
    calls: list[int] = []
    k = key("demo", uuid.uuid4(), "x")

    def load() -> dict:
        calls.append(1)
        return {"v": 1}

    for _ in range(3):
        with request_scope():
            assert shared.cached(k, load, ttl_seconds=60, encode=lambda v: v, decode=lambda v: v) == {"v": 1}
    assert len(calls) == 1
    shared.invalidate(k)
    with request_scope():
        shared.cached(k, load, ttl_seconds=60, encode=lambda v: v, decode=lambda v: v)
    assert len(calls) == 2


def test_an_unreadable_entry_is_dropped_and_reloaded(monkeypatch) -> None:
    backend = shared.MemoryBackend()
    monkeypatch.setattr(shared, "get_backend", lambda: backend)
    k = key("demo", uuid.uuid4())
    backend.set(k.render(), b'{"shape": "old"}', 60)
    value = shared.cached(k, lambda: {"v": 2}, ttl_seconds=60, encode=lambda v: v, decode=lambda raw: {"v": raw["v"]})
    assert value == {"v": 2}
