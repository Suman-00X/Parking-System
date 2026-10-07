"""Sharding by lab across two real databases: a lab's rows live on its shard, its row is mirrored, isolation holds on each shard, reads fan out."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from radreport.core.config import get_settings
from radreport.db import sharding
from radreport.db.session import system_session, tenant_session
from tests.conftest import _purge_tenants
from tests.db.helpers import make_platform_user, signed_in

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get("RADREPORT_TEST_SHARD_URL"), reason="set RADREPORT_TEST_SHARD_URL to a second migrated database")]


@pytest.fixture
def shards(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    second = os.environ["RADREPORT_TEST_SHARD_URL"]
    monkeypatch.setenv("RADREPORT_DB__SHARDS", json.dumps({"a": migrated_db, "b": second}))
    get_settings.cache_clear()
    sharding.reset()
    created: list[uuid.UUID] = []
    yield {"a": migrated_db, "b": second, "created": created}
    for url in (migrated_db, second):
        _purge_tenants(url, created)
    monkeypatch.delenv("RADREPORT_DB__SHARDS")
    get_settings.cache_clear()
    sharding.reset()


def _count(url: str, sql: str, **params: object) -> int:
    with create_engine(url).connect() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant_id', :t, false)"), {"t": str(params.get("t", ""))})
        return int(conn.execute(text(sql), params).scalar_one())


def _register(client, n: int) -> uuid.UUID:  # type: ignore[no-untyped-def]
    slug = f"shard-{uuid.uuid4().hex[:8]}"
    response = client.post("/admin/api/labs", json={"name": f"Shard lab {n}", "slug": slug, "admin_email": f"{slug}@lab.example", "admin_display_name": "Lab Admin", "admin_employee_code": "LA1"})
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def test_labs_live_on_their_shard_and_stay_isolated(shards, migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db))
    labs = [_register(client, n) for n in range(8)]
    shards["created"].extend(labs)
    on_b = [lab for lab in labs if sharding.lab_shard(lab) == "b"]
    on_a = [lab for lab in labs if sharding.lab_shard(lab) == "a"]
    assert on_a and on_b, "eight labs over two shards should use both"
    lab, neighbour = on_b[0], (on_b[1:] or [None])[0]

    # The lab's own rows are on its shard only; its lab row is on both.
    assert _count(shards["b"], "SELECT count(*) FROM app_user WHERE tenant_id = :t", t=lab) == 1
    assert _count(shards["a"], "SELECT count(*) FROM app_user WHERE tenant_id = :t", t=lab) == 0
    for url in (shards["a"], shards["b"]):
        assert _count(url, "SELECT count(*) FROM tenant WHERE id = :t", t=lab) == 1

    # A lab session writes to the shard; the directory's lab list still shows every lab.
    with tenant_session(lab) as session:
        session.execute(text("INSERT INTO app_user (tenant_id, employee_code, display_name, roles) VALUES (:t, 'X9', 'Shard Writer', ARRAY['radiologist'])"), {"t": lab})
    assert _count(shards["b"], "SELECT count(*) FROM app_user WHERE tenant_id = :t", t=lab) == 2
    # The admin panel's lab page reads from the directory and still finds a lab whose data lives elsewhere.
    assert client.get(f"/admin/labs/{lab}").status_code == 200

    # A status change made on the shard reaches the directory copy.
    assert client.post(f"/admin/api/labs/{lab}/status", json={"status": "offboarded"}).status_code == 200
    with system_session() as session:
        assert session.execute(text("SELECT status FROM tenant WHERE id = :t"), {"t": lab}).scalar_one() == "offboarded"

    # Row-level security holds on the shard as on any database.
    if neighbour is not None:
        with tenant_session(neighbour) as session:
            assert session.execute(text("SELECT count(*) FROM app_user WHERE tenant_id = :t"), {"t": lab}).scalar_one() == 0


def test_cost_reads_fan_out_across_shards(shards, migrated_db: str) -> None:
    import datetime as dt

    from radreport.devtools.cost_history import write_history
    from radreport.monitoring import costs

    client = signed_in(make_platform_user(migrated_db))
    labs = [_register(client, n) for n in range(6)]
    shards["created"].extend(labs)
    lab_b = next(lab for lab in labs if sharding.lab_shard(lab) == "b")
    lab_a = next(lab for lab in labs if sharding.lab_shard(lab) == "a")
    today = dt.date(2026, 6, 10)
    write_history(lab_b, days=5, url=shards["b"], today=today)
    write_history(lab_a, days=5, url=shards["a"], today=today)
    with system_session() as session:
        summary = costs.platform_summary(session, days=7, today=today)
    assert {entry.tenant_id for entry in summary["labs"]} >= {lab_a, lab_b}


def test_readiness_checks_every_shard(shards) -> None:
    from fastapi.testclient import TestClient

    from radreport.api.app import create_app

    body = TestClient(create_app()).get("/ready").json()
    assert body["checks"]["shard:a"] == "ok" and body["checks"]["shard:b"] == "ok"
