"""Fixtures shared by the whole suite.

Database-backed tests are marked with @pytest.mark.db and skip unless
RADREPORT_TEST_DATABASE_URL is set, so the suite runs with no database present.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest


def _test_db_url() -> str | None:
    return os.environ.get("RADREPORT_TEST_DATABASE_URL")


def _app_db_url() -> str | None:
    """Connection as the least-privileged app role, for the isolation tests."""
    return os.environ.get("RADREPORT_TEST_APP_DATABASE_URL") or _test_db_url()


requires_db = pytest.mark.skipif(_test_db_url() is None, reason="set RADREPORT_TEST_DATABASE_URL to run DB-backed tests")


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _test_db_url()
    if url is None:
        pytest.skip("no RADREPORT_TEST_DATABASE_URL")
    return url


@pytest.fixture(scope="session")
def app_db_url() -> str:
    url = _app_db_url()
    if url is None:
        pytest.skip("no RADREPORT_TEST_DATABASE_URL")
    return url


@pytest.fixture(scope="session")
def migrated_db(db_url: str) -> str:
    """Run migrations once per session."""
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")
    return db_url


@pytest.fixture
def two_tenants(migrated_db: str) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two tenants with a little data each — the leak test's fixture."""
    from radreport.db.models.tenancy import Tenant
    from radreport.db.session import system_session

    ids: list[uuid.UUID] = []
    with system_session(migrated_db) as session:
        for slug in ("leak-test-a", "leak-test-b"):
            tenant = Tenant(name=slug, slug=f"{slug}-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            session.flush()
            ids.append(tenant.id)

    yield ids[0], ids[1]

    _purge_tenants(migrated_db, ids)


def _purge_tenants(url: str, tenant_ids: list[uuid.UUID]) -> None:
    """Delete a tenant's rows, children first."""
    from sqlalchemy import text

    from radreport.db.introspect import tenant_scoped_tables
    from radreport.db.models import Base
    from radreport.db.session import get_engine

    order = [t.name for t in reversed(Base.metadata.sorted_tables)]
    scoped = set(tenant_scoped_tables()) - {"audit_log"}

    engine = get_engine(url)
    for tenant_id in tenant_ids:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)})
            for table in order:
                if table in scoped:
                    conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": str(tenant_id)})
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant_id', '', true)"))
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": str(tenant_id)})


@pytest.fixture
def clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the settings cache so env overrides take effect."""
    from radreport.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_shared_rate_limits(request: pytest.FixtureRequest) -> Iterator[None]:
    """Shared rate-limit counters live in the database, so one test's sign-ins would throttle the next."""
    if "migrated_db" in request.fixturenames:
        from sqlalchemy import text

        from radreport.db.session import system_session

        with system_session(request.getfixturevalue("migrated_db")) as session:
            session.execute(text("DELETE FROM rate_limit_counter"))
    yield
