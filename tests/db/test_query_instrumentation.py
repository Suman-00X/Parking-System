"""Query instrumentation against a real database: per-request counts, the slow-query log, and the ops endpoint."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from radreport.core.config import get_settings
from radreport.core.types import PlatformRole
from radreport.db import instrumentation
from radreport.db.session import system_session
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db


def test_a_slow_statement_is_logged_without_its_parameters(migrated_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The slow-query log names the statement and its time, never the bound values."""
    monkeypatch.setattr(get_settings().db, "slow_query_ms", 20.0)
    with instrumentation.query_scope("slow-test") as stats, system_session(migrated_db) as session:
        session.execute(text("SELECT pg_sleep(0.05), :secret"), {"secret": "MRN-0042"})
    out = capsys.readouterr().out
    assert stats.slow >= 1
    assert "slow_query" in out
    assert "pg_sleep" in out
    assert "MRN-0042" not in out


def test_every_response_says_how_many_statements_it_ran(migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db))
    page = client.get("/admin/labs")
    assert page.status_code == 200
    assert int(page.headers["x-query-count"]) >= 1
    assert page.headers["server-timing"].startswith("db;dur=")


def test_the_ops_endpoint_reports_percentiles_by_route(migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db, role=PlatformRole.SUPPORT))
    client.get("/admin/labs")
    body = client.get("/admin/api/ops/queries").json()
    assert body["queries_total"] > 0
    assert {"p50", "p95", "p99"} <= set(body["query_ms"])
    assert any(r["route"] == "admin.labs.page" for r in body["routes"])
