"""Through PgBouncer in transaction mode: many client connections share few server connections, and lab binding never leaks between them."""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get("RADREPORT_TEST_PGBOUNCER_URL"), reason="set RADREPORT_TEST_PGBOUNCER_URL (make pgbouncer) to run")]

CLIENTS = 200


def test_two_hundred_clients_share_a_small_server_pool() -> None:
    engine = create_engine(os.environ["RADREPORT_TEST_PGBOUNCER_URL"], poolclass=NullPool, connect_args={"prepare_threshold": None})
    backends: set[int] = set()
    leaks: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(CLIENTS)

    def client() -> None:
        mine = str(uuid.uuid4())
        with engine.begin() as conn:
            start.wait(timeout=30)
            conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": mine})
            pid, seen = conn.execute(text("SELECT pg_backend_pid(), current_setting('app.current_tenant_id'), pg_sleep(0.05)")).one()[:2]
        with engine.begin() as conn:
            # A later transaction, perhaps on another client's old server connection, must start with no lab bound.
            after = conn.execute(text("SELECT current_setting('app.current_tenant_id', true)")).scalar()
        with lock:
            backends.add(pid)
            if seen != mine or after:
                leaks.append(f"{mine} saw {seen} then {after!r}")

    threads = [threading.Thread(target=client) for _ in range(CLIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not leaks, leaks[:3]
    assert len(backends) <= 25, f"{CLIENTS} clients used {len(backends)} server connections"
