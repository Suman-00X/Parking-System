"""The shared cache against a real database: revocation is immediate, writes invalidate on commit, hit rate in steady state, and Redis when one is running."""

from __future__ import annotations

import os
import uuid

import pytest

from radreport.cache import lookups, shared
from radreport.core.types import PlatformRole, TenantStatus
from radreport.db.session import system_session, tenant_session
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db


def test_logging_out_ends_a_cached_session_at_once(migrated_db: str) -> None:
    from fastapi.testclient import TestClient

    from radreport.api.app import create_app
    from tests.db.helpers import PASSWORD

    email = make_platform_user(migrated_db)
    app = create_app()
    cookie = TestClient(app).post("/admin/login", data={"email": email, "password": PASSWORD}, follow_redirects=False).cookies["radreport_admin"]
    holder = TestClient(app, cookies={"radreport_admin": cookie})
    assert holder.get("/admin/api/labs").status_code == 200  # now cached
    TestClient(app, cookies={"radreport_admin": cookie}).post("/admin/logout")
    assert holder.get("/admin/api/labs").status_code == 401


def test_deactivating_an_admin_ends_their_cached_sessions(migrated_db: str) -> None:
    from radreport.admin import users

    email = make_platform_user(migrated_db, role=PlatformRole.SUPPORT)
    victim = signed_in(email)
    assert victim.get("/admin/api/labs").status_code == 200
    with system_session(migrated_db) as session:
        target = next(u for u in users.list_platform_users(session) if u.email == email)
        users.set_active(session, user_id=target.id, active=False, actor_id=uuid.uuid4())
    assert victim.get("/admin/api/labs").status_code == 401


def test_a_status_change_invalidates_the_lab_config_on_commit(migrated_db: str, two_tenants) -> None:
    from radreport.onboarding.registration import transition_status

    tenant_id, _ = two_tenants
    with system_session(migrated_db) as session:
        assert lookups.tenant_config(session, tenant_id).status == TenantStatus.PROVISIONING
    with tenant_session(tenant_id, url=migrated_db) as session:
        transition_status(session, tenant_id, TenantStatus.ONBOARDING)
    with system_session(migrated_db) as session:
        assert lookups.tenant_config(session, tenant_id).status == TenantStatus.ONBOARDING


def test_steady_state_hit_rate_is_above_eighty_percent(migrated_db: str, two_tenants) -> None:
    """The validation target: repeated admin traffic is served from the caches, not the database."""
    tenant_id, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    client.get(f"/admin/labs/{tenant_id}")
    shared.STATS.reset()
    for _ in range(25):
        assert client.get(f"/admin/labs/{tenant_id}").status_code == 200
    assert shared.STATS.hit_rate > 0.8, shared.STATS.as_dict()


@pytest.mark.skipif(not os.environ.get("RADREPORT_TEST_REDIS_URL"), reason="set RADREPORT_TEST_REDIS_URL to run against Redis")
def test_redis_shares_values_and_invalidations(migrated_db: str, two_tenants) -> None:
    backend = shared.RedisBackend(os.environ["RADREPORT_TEST_REDIS_URL"])
    assert backend.ping() and backend.shared
    k = f"radreport:test:{uuid.uuid4()}"
    backend.set(k, b"x", 5)
    assert backend.get(k) == b"x"
    backend.delete(k)
    assert backend.get(k) is None
