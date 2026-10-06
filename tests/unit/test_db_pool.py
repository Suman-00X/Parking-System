"""The engine's pool is sized from settings, and PgBouncer mode turns off prepared statements."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from radreport.core.config import get_settings
from radreport.db.session import engine_options


def test_the_default_pool_is_sized_for_peak_load(clean_settings) -> None:
    """Thirty kept connections per worker, plus a bounded burst."""
    options = engine_options()
    assert options["pool_size"] == 30
    assert options["max_overflow"] == 10
    assert options["pool_timeout"] == 10.0
    assert "connect_args" not in options


def test_pool_settings_come_from_the_environment(clean_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ops can resize the pool without a code change."""
    monkeypatch.setenv("RADREPORT_DB__POOL_SIZE", "45")
    monkeypatch.setenv("RADREPORT_DB__MAX_OVERFLOW", "5")
    get_settings.cache_clear()
    engine = create_engine("postgresql+psycopg://u:p@localhost/x", **engine_options())
    assert engine.pool.size() == 45
    assert engine.pool._max_overflow == 5  # noqa: SLF001 - no public accessor


def test_pgbouncer_mode_disables_server_side_prepared_statements(clean_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """In transaction pooling the next transaction may land on a server that never saw the statement."""
    monkeypatch.setenv("RADREPORT_DB__PGBOUNCER", "true")
    get_settings.cache_clear()
    assert engine_options()["connect_args"] == {"prepare_threshold": None}
