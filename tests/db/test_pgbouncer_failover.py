"""PgBouncer dies and comes back: requests in between fail fast with 503 Retry-After, and the app recovers without a restart."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from radreport.core.config import get_settings
from radreport.db.session import get_engine, system_session
from tests.db.helpers import make_platform_user, signed_in

pytestmark = [pytest.mark.db, pytest.mark.skipif(shutil.which("pgbouncer") is None, reason="needs the pgbouncer binary (brew install pgbouncer)")]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Bouncer:
    """A throwaway PgBouncer in transaction mode in front of the test database."""

    def __init__(self, directory: Path, db_url: str) -> None:
        url = make_url(db_url)
        self.port = _free_port()
        self.url = url.set(host="127.0.0.1", port=self.port).render_as_string(hide_password=False)
        (directory / "userlist.txt").write_text(f'"{url.username}" "{url.password}"\n')
        self.ini = directory / "pgbouncer.ini"
        self.ini.write_text(f"[databases]\n{url.database} = host={url.host or '127.0.0.1'} port={url.port or 5432} dbname={url.database}\n[pgbouncer]\nlisten_addr = 127.0.0.1\nlisten_port = {self.port}\nunix_socket_dir =\nauth_type = scram-sha-256\nauth_file = {directory / 'userlist.txt'}\npool_mode = transaction\ndefault_pool_size = 10\nignore_startup_parameters = extra_float_digits,options\n")
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(["pgbouncer", str(self.ini)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError("pgbouncer did not start")

    def crash(self) -> None:
        """SIGKILL, as an OOM kill or a lost host would: no clean shutdown, every connection cut."""
        assert self.process is not None
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=10)


@pytest.fixture
def bouncer(tmp_path: Path, migrated_db: str, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    pool = Bouncer(tmp_path, migrated_db)
    pool.start()
    monkeypatch.setenv("RADREPORT_DATABASE_URL", pool.url)
    monkeypatch.setenv("RADREPORT_DB__PGBOUNCER", "true")
    monkeypatch.setenv("RADREPORT_DB__CONNECT_TIMEOUT_SECONDS", "2")
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield pool
    if pool.process and pool.process.poll() is None:
        pool.crash()  # SIGTERM waits for clients to leave; the test is done with them
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_requests_fail_fast_while_pgbouncer_is_down_and_recover_when_it_returns(bouncer: Bouncer, migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    path = f"/admin/api/labs/{lab}/readiness"
    assert client.get(path).status_code == 200

    bouncer.crash()
    started = time.monotonic()
    down = client.get(path)
    assert down.status_code == 503 and down.headers["retry-after"] == "5", down.text[:200]
    assert time.monotonic() - started < 8, "a dead pooler must fail the request within the connect timeout, not hang"

    bouncer.start()
    assert client.get(path).status_code == 200, "the pools drop dead connections (pool_pre_ping) and reconnect without a restart"


def test_the_sync_engine_recovers_too(bouncer: Bouncer) -> None:
    from sqlalchemy.exc import OperationalError

    with system_session() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
    bouncer.crash()
    with pytest.raises(OperationalError), system_session() as session:
        session.execute(text("SELECT 1"))
    bouncer.start()
    with system_session() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
    assert os.environ["RADREPORT_DB__PGBOUNCER"] == "true"
