"""The per-request cache: one load per key inside a request, none shared between requests or labs."""

from __future__ import annotations

import uuid

import pytest

from radreport.cache.keys import GLOBAL, key
from radreport.cache.request import STATS, forget, request_cached, request_scope


def test_a_repeated_lookup_inside_a_request_loads_once() -> None:
    calls: list[int] = []
    tenant = uuid.uuid4()
    with request_scope():
        for _ in range(5):
            assert request_cached(key("roles", tenant, "u1"), lambda: calls.append(1) or ("radiologist",)) == ("radiologist",)
    assert len(calls) == 1


def test_nothing_survives_the_request() -> None:
    calls: list[int] = []
    tenant = uuid.uuid4()
    for _ in range(2):
        with request_scope():
            request_cached(key("roles", tenant, "u1"), lambda: calls.append(1))
    assert len(calls) == 2


def test_outside_a_request_every_call_loads() -> None:
    calls: list[int] = []
    for _ in range(3):
        request_cached(key("roles", uuid.uuid4(), "u1"), lambda: calls.append(1))
    assert len(calls) == 3


def test_two_labs_never_share_a_value() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    with request_scope():
        assert request_cached(key("tenant_config", a), lambda: "lab a") == "lab a"
        assert request_cached(key("tenant_config", b), lambda: "lab b") == "lab b"


def test_a_key_without_a_lab_is_refused() -> None:
    with pytest.raises(ValueError, match="no tenant"):
        key("tenant_config", None)  # type: ignore[arg-type]
    assert key("admin_session", GLOBAL, "abc").render() == "radreport:admin_session:global:abc"


def test_forget_drops_a_value_the_request_changed() -> None:
    tenant = uuid.uuid4()
    with request_scope():
        request_cached(key("tenant_config", tenant), lambda: "before")
        forget(key("tenant_config", tenant))
        assert request_cached(key("tenant_config", tenant), lambda: "after") == "after"


def test_the_hit_rate_is_counted() -> None:
    STATS.reset()
    tenant = uuid.uuid4()
    with request_scope():
        for _ in range(10):
            request_cached(key("roles", tenant), lambda: 1)
    assert STATS.as_dict() == {"hits": 9, "misses": 1, "hit_rate": 0.9}
