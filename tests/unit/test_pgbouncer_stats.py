"""The pool dashboard without a PgBouncer: it says why, and a pool near its limit is flagged busy."""

from __future__ import annotations

import pytest

from radreport.core.config import get_settings
from radreport.db import pgbouncer_stats


def test_without_pgbouncer_the_report_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADREPORT_DB__PGBOUNCER", "false")
    monkeypatch.delenv("RADREPORT_DB__PGBOUNCER_ADMIN_URL", raising=False)
    get_settings.cache_clear()
    try:
        report = pgbouncer_stats.pool_report()
    finally:
        get_settings.cache_clear()
    assert report == {"enabled": False, "reason": "not behind PgBouncer (RADREPORT_DB__PGBOUNCER is off)", "pools": []}


def test_the_console_is_the_app_database_url_on_the_pgbouncer_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADREPORT_DB__PGBOUNCER", "true")
    monkeypatch.setenv("RADREPORT_DATABASE_URL", "postgresql+psycopg://app:pw@127.0.0.1:6432/radreport")
    monkeypatch.delenv("RADREPORT_DB__PGBOUNCER_ADMIN_URL", raising=False)
    get_settings.cache_clear()
    try:
        assert pgbouncer_stats.admin_url() == "postgresql://app:pw@127.0.0.1:6432/pgbouncer"
    finally:
        get_settings.cache_clear()


def test_an_unreachable_console_is_reported_not_raised() -> None:
    report = pgbouncer_stats.pool_report("postgresql://nobody:x@127.0.0.1:1/pgbouncer")
    assert report["enabled"] and not report["reachable"] and report["pools"] == []


def test_a_pool_near_its_limit_is_busy() -> None:
    row = {"database": "radreport", "user": "app", "cl_active": 40, "cl_waiting": 3, "sv_active": 17, "sv_idle": 0, "sv_used": 1, "maxwait": 1, "maxwait_us": 250000, "pool_mode": "transaction"}
    pool = pgbouncer_stats._pool(row, 20)
    assert pool.busy and pool.clients_waiting == 3 and pool.max_wait_ms == 1250.0
    assert not pgbouncer_stats._pool(row | {"sv_active": 10}, 20).busy
