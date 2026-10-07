"""Read routing: read-only pages and exports go to the replica, writes and read-your-writes stay on the primary, lag sends reads home."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from radreport.core.config import get_settings
from radreport.db import instrumentation
from radreport.db import session as db_session
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db


@pytest.fixture
def replica(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """The same database under another host name, so its statements are told apart. A real deployment points this at a streaming replica."""
    url = migrated_db.replace("@localhost", "@127.0.0.1") if "@localhost" in migrated_db else migrated_db.replace("@127.0.0.1", "@localhost")
    monkeypatch.setenv("RADREPORT_DB__REPLICA_URL", url)
    get_settings.cache_clear()
    db_session._lag_checked.clear()
    yield url
    monkeypatch.delenv("RADREPORT_DB__REPLICA_URL")
    get_settings.cache_clear()
    db_session._lag_checked.clear()


def test_without_a_replica_reads_use_the_primary(migrated_db: str) -> None:
    assert db_session.replica_url_for_reads() is None


def test_read_only_pages_read_from_the_replica(replica: str, migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db))
    client.cookies.delete("radreport_wrote")  # signing in is a write; its read-your-writes window is not what this measures
    for path in ("/admin/labs", "/admin/providers", "/admin/users", "/admin/api/labs", "/admin/api/users", "/admin/api/ops/tables"):
        client.get(path)  # warm the session and lookup caches
    instrumentation.METRICS.reset()
    for _ in range(3):
        for path in ("/admin/labs", "/admin/providers", "/admin/users", "/admin/api/labs", "/admin/api/users", "/admin/api/ops/tables"):
            assert client.get(path).status_code == 200
    snapshot = instrumentation.METRICS.snapshot()
    assert snapshot["replica_read_share"] >= 0.9, snapshot["reads_by_target"]


def test_a_browser_that_just_wrote_reads_from_the_primary(replica: str, migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db))
    client.cookies.delete("radreport_wrote")
    email = f"new-reader-{uuid.uuid4().hex[:8]}@example.com"
    written = client.post("/admin/users", data={"display_name": "New", "email": email, "role": "support", "password": "a long enough password"})
    assert written.status_code == 303 and "radreport_wrote" in written.headers.get("set-cookie", "")
    instrumentation.METRICS.reset()
    assert client.get("/admin/users").status_code == 200
    assert instrumentation.METRICS.snapshot()["reads_by_target"].get("replica", 0) == 0, "the page right after the write reads the primary"
    client.cookies.delete("radreport_wrote")
    instrumentation.METRICS.reset()
    client.get("/admin/users")
    assert instrumentation.METRICS.snapshot()["reads_by_target"].get("replica", 0) > 0, "and the replica again once the window has passed"


def test_a_lagging_or_unreachable_replica_is_skipped(replica: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_session, "replica_lag_seconds", lambda _url: 120.0)
    assert db_session.replica_url_for_reads() is None
    db_session._lag_checked.clear()

    def unreachable(_url: str) -> float:
        raise ConnectionError("replica down")

    monkeypatch.setattr(db_session, "replica_lag_seconds", unreachable)
    assert db_session.replica_url_for_reads() is None
    db_session._lag_checked.clear()
    monkeypatch.setattr(db_session, "replica_lag_seconds", lambda _url: 0.5)
    assert db_session.replica_url_for_reads() == replica


def test_a_read_session_cannot_write(replica: str) -> None:
    with pytest.raises(DBAPIError, match="read-only"), db_session.read_session() as session:
        session.execute(text("DELETE FROM rate_limit_counter"))


def test_an_export_the_replica_has_not_seen_comes_from_the_primary(replica: str, monkeypatch: pytest.MonkeyPatch, two_tenants) -> None:
    from radreport.api.routes import ga

    tenant_id, _ = two_tenants
    calls: list[bool] = []

    def load(session, tenant, report):  # type: ignore[no-untyped-def]
        calls.append(bool(session.info.get("read_only")))
        return None if session.info.get("read_only") else ("final", None, None, None)

    monkeypatch.setattr(ga, "_load_export", load)
    with db_session.read_session(tenant_id) as session:
        assert ga._export_context(session, tenant_id, uuid.uuid4())[0] == "final"
    assert calls == [True, False]
