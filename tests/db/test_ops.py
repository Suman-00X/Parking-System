"""The liveness and readiness endpoints (/health, /ready)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radreport.api.app import create_app

pytestmark = pytest.mark.db


def test_liveness_checks_only_that_the_process_is_up(migrated_db: str) -> None:
    """A liveness probe that fails on a brief database blip gets the container killed, which does not reconnect the database and does lose every in-flight request."""
    client = TestClient(create_app())
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["checks"]["database"]["ok"] and body["checks"]["database"]["latency_ms"] >= 0
    assert {"pool", "memory"} <= set(body["checks"])
    assert body["instance_id"] and response.headers["x-instance-id"] == body["instance_id"]


def test_the_instance_id_comes_from_the_environment_when_set(monkeypatch) -> None:
    """A load balancer names its targets; the app reports the same name back."""
    from radreport.api.routes import health

    monkeypatch.setenv("INSTANCE_ID", "web-7")
    health.instance_id.cache_clear()
    try:
        assert health.instance_id() == "web-7"
    finally:
        health.instance_id.cache_clear()


def test_health_writes_nothing(migrated_db: str, monkeypatch) -> None:
    """A probe runs every few seconds on every instance; it must not write."""
    from radreport.db import instrumentation

    seen: list[str] = []
    original = instrumentation.QueryStats.record

    def spy(self, ms, statement, *, slow):  # type: ignore[no-untyped-def]
        seen.append(statement)
        original(self, ms, statement, slow=slow)

    monkeypatch.setattr(instrumentation.QueryStats, "record", spy)
    TestClient(create_app()).get("/health")
    assert seen, "the probe should have touched the database"
    assert not [s for s in seen if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))], seen


def test_a_registered_check_shows_in_health_and_gates_readiness(migrated_db: str) -> None:
    from radreport.api.routes import health

    health.register_check("example", lambda: {"ok": False, "error": "down for the test"})
    try:
        client = TestClient(create_app())
        assert client.get("/health").json()["status"] == "degraded"
        ready = client.get("/ready")
        assert ready.status_code == 503 and ready.json()["checks"]["example"] == "down for the test"
    finally:
        health._CHECKS.pop("example", None)


def test_readiness_checks_the_database_and_the_schema_version(migrated_db: str) -> None:
    client = TestClient(create_app())
    response = client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["database"] == "ok"
    # A process running against an older schema than its code presents as
    # random column errors hours later, so the revision is part of readiness.
    assert payload["checks"]["migrations"].startswith("at ")


def test_readiness_fails_on_an_unreachable_database(monkeypatch) -> None:
    """The case `/health` cannot see."""
    from radreport.core.config import get_settings
    from radreport.db.session import get_engine

    monkeypatch.setenv("RADREPORT_DATABASE_URL", "postgresql+psycopg://nobody:wrong@127.0.0.1:5432/does_not_exist")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        # Liveness still passes — the process is fine — but says which dependency is not.
        live = client.get("/health")
        assert live.status_code == 200
        assert live.json()["status"] == "degraded" and not live.json()["checks"]["database"]["ok"]

        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert "OperationalError" in response.json()["checks"]["database"]
    finally:
        monkeypatch.delenv("RADREPORT_DATABASE_URL", raising=False)
        get_settings.cache_clear()
        get_engine.cache_clear()
