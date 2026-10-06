"""Rate limits whose count every worker shares, against a real database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radreport.api.access import RateLimit, SharedRateLimiter
from radreport.api.app import create_app

pytestmark = pytest.mark.db


def test_two_limiters_share_one_count(migrated_db: str) -> None:
    """Two instances stand in for two worker processes."""
    limit = RateLimit(id="test-shared", requests=3, window_seconds=60, key="ip", store="shared")
    first, second = SharedRateLimiter(), SharedRateLimiter()
    results = [first.hit(limit, "ip:9.9.9.9"), second.hit(limit, "ip:9.9.9.9"), first.hit(limit, "ip:9.9.9.9"), second.hit(limit, "ip:9.9.9.9")]
    assert results[:3] == [None, None, None]
    assert results[3] is not None and results[3] > 0


def test_password_guessing_is_limited_across_app_instances(migrated_db: str) -> None:
    """Five tries a minute per address, however many workers the guesses are spread over."""
    workers = [TestClient(create_app(), follow_redirects=False) for _ in range(2)]
    statuses = [workers[i % 2].post("/auth/login", json={"lab": "no-such-lab", "email": "x@y.z", "password": "guess"}).status_code for i in range(7)]
    assert statuses[:5] == [401] * 5
    assert statuses[5:] == [429, 429]
