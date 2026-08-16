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
    assert response.json()["status"] == "ok"


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
        # Liveness still passes — the process is fine.
        assert client.get("/health").status_code == 200

        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert "OperationalError" in response.json()["checks"]["database"]
    finally:
        monkeypatch.delenv("RADREPORT_DATABASE_URL", raising=False)
        get_settings.cache_clear()
        get_engine.cache_clear()
