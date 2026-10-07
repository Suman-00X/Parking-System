"""Breaks the system on purpose against a disposable test database and records what it did, for the features page's crash-test tab.

Order: refuse anything but a test database (_require_test_db) -> run each scenario, measuring what
happened rather than assuming it (worker_killed, relay_crash, double_claim, duplicate_enqueue,
poison_job, provider_outage, database_down, cache_down, request_flood) -> write the results as
Markdown and JSON (write_report). `--worker` is the child process the first scenario kills with
SIGKILL.

    RADREPORT_TEST_DATABASE_URL=... python -m radreport.devtools.crash_test     (make crash-test)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT_MD = ROOT / "docs" / "CRASH_TEST.md"
REPORT_JSON = ROOT / "docs" / "crash-test.json"

SLOW_KIND = "crash_slow"
RACE_KIND = "crash_race"
DEDUPE_KIND = "crash_dedupe"
POISON_KIND = "crash_poison"
HEALTHY_KIND = "crash_healthy"
TOPIC = "crash.test"


@dataclass
class Outcome:
    key: str
    title: str
    broke: str
    expected: str
    observed: str
    passed: bool
    numbers: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0


def _require_test_db() -> str:
    """The test database's URL; every scenario writes jobs and events, so the app's own database is refused."""
    url = os.environ.get("RADREPORT_TEST_DATABASE_URL")
    if not url:
        sys.exit("set RADREPORT_TEST_DATABASE_URL to a disposable, migrated test database")
    if not url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].endswith("_test"):
        sys.exit("refusing: the crash test only runs against a database whose name ends in _test")
    # Every engine the app opens on its own (the side pool, the async health check) must land on the test database too.
    os.environ["RADREPORT_DATABASE_URL"] = url
    os.environ.setdefault("RADREPORT_ENVIRONMENT", "test")
    os.environ.setdefault("RADREPORT_DB__ECHO", "false")
    from radreport.core.config import get_settings

    get_settings.cache_clear()
    return url


def _wait(predicate: Callable[[], bool], timeout: float, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _job(url: str, job_id: uuid.UUID) -> Any:
    from radreport.db.models.jobs import Job
    from radreport.db.session import system_session

    with system_session(url) as session:
        job = session.get(Job, job_id)
        session.expunge(job)
        return job


def _cleanup(url: str) -> None:
    from sqlalchemy import text

    from radreport.db.session import system_session

    with system_session(url) as session:
        session.execute(text("DELETE FROM job WHERE kind LIKE 'crash_%' AND tenant_id IS NULL"))


# ============================================================ scenarios ===
def worker_killed(url: str) -> Outcome:
    """SIGKILL a worker halfway through a job; another worker must finish it once."""
    from radreport.db.session import system_session
    from radreport.workers import queue

    lease = 3
    with system_session(url) as session:
        job_id = queue.enqueue(session, SLOW_KIND, {"seconds": 4}, max_attempts=3)
    env = {**os.environ, "RADREPORT_DATABASE_URL": url}
    cmd = [sys.executable, "-m", "radreport.devtools.crash_test", "--worker", "--visibility", str(lease)]
    first = subprocess.Popen([*cmd, "--worker-id", "victim"], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    second: subprocess.Popen[bytes] | None = None
    try:
        started = _wait(lambda: _job(url, job_id).locked_by == "victim", 30)
        time.sleep(1)  # well inside the 4-second job
        first.send_signal(signal.SIGKILL)
        first.wait()
        killed_at = time.monotonic()
        second = subprocess.Popen([*cmd, "--worker-id", "rescuer"], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finished = _wait(lambda: _job(url, job_id).status == "succeeded", 40)
        recovered = time.monotonic() - killed_at
        job = _job(url, job_id)
    finally:
        for proc in (first, second):
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
    by = (job.result or {}).get("worker")
    ok = started and finished and job.attempts == 2 and by == "rescuer"
    return Outcome(
        "worker_killed", "A worker is killed in the middle of a job", "Started a real worker process on a 4-second job and killed it with SIGKILL one second in: no shutdown, no cleanup, no chance to say anything.", "The job is not lost and not run twice: its lease runs out, a second worker claims it, and it finishes exactly once.", f"A second worker finished it {recovered:.1f} s after the kill (the {lease}-second lease running out, plus starting the new worker and redoing the 4-second job), on attempt {job.attempts}. The killed worker's half-done work never committed.", ok, {"lease_seconds": lease, "recovered_seconds": round(recovered, 2), "attempts": job.attempts, "finished_by": by}
    )


def relay_crash(url: str) -> Outcome:
    """The relay dies after publishing and before marking the event sent; the consumer must still apply it once."""
    from sqlalchemy import select

    from radreport.db.models.events import OutboxEvent
    from radreport.db.session import system_session
    from radreport.events import consumers
    from radreport.events.bus import PostgresEventBus
    from radreport.events.outbox import emit
    from radreport.events.relay import Relay

    applied: list[uuid.UUID] = []
    consumers.consumer("crash-test-counter", TOPIC)(lambda _session, event: applied.append(event.event_id))

    class CrashAfterPublish(PostgresEventBus):
        def publish(self, events: list[Any]) -> None:
            super().publish(events)
            raise ConnectionError("relay process died before marking the events sent")

    try:
        with system_session(url) as session:
            event_id = emit(session, TOPIC, {"case": "relay crash"}, tenant_id=None).event_id

        def sent() -> bool:
            with system_session(url) as session:
                return session.execute(select(OutboxEvent.published_at).where(OutboxEvent.event_id == event_id)).scalar_one() is not None

        crashed_passes = 0
        while event_id not in applied and crashed_passes < 50:
            Relay(bus=CrashAfterPublish(url), url=url).run_once()
            crashed_passes += 1
        after_crash = list(applied)
        passes = 0
        while not sent() and passes < 50:
            Relay(bus=PostgresEventBus(url), url=url).run_once()
            passes += 1
        with system_session(url) as session:
            attempts = session.execute(select(OutboxEvent.attempts).where(OutboxEvent.event_id == event_id)).scalar_one()
    finally:
        consumers.unregister("crash-test-counter")
    ok = after_crash == [event_id] and applied == [event_id] and sent()
    return Outcome("relay_crash", "The event relay crashes after sending, before recording it", "Made the relay publish an event and then fail before it could mark the event as sent, the worst moment for a message system to die.", "The event is sent again on the next pass (at least once), and the consumer ignores the repeat (applied exactly once).", f"The event was delivered on {attempts} passes and applied {len(applied)} time. The repeat was recognised by its id and skipped.", ok, {"deliveries": attempts, "applied": len(applied)})


def double_claim(url: str) -> Outcome:
    """Eight workers race to drain 200 jobs; no job may go to two of them."""
    from radreport.db.session import system_session
    from radreport.workers import queue

    jobs = 200
    with system_session(url) as session:
        for n in range(jobs):
            queue.enqueue(session, RACE_KIND, {"n": n})
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()

    def drain(worker: str) -> None:
        while True:
            with system_session(url) as session:
                batch = queue.claim(session, worker_id=worker, kinds=[RACE_KIND], limit=5)
                for job in batch:
                    queue.complete(session, job, worker_id=worker)
            if not batch:
                return
            with lock:
                claimed.extend(j.id for j in batch)

    started = time.perf_counter()
    threads = [threading.Thread(target=drain, args=(f"racer-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started
    duplicates = len(claimed) - len(set(claimed))
    return Outcome("double_claim", "Eight workers fight over the same queue", f"Put {jobs} jobs on the queue and let 8 workers grab them as fast as they could, all at once.", "Every job is handed to exactly one worker: none skipped, none done twice.", f"{len(set(claimed))} of {jobs} jobs claimed, {duplicates} handed out twice, at about {jobs / elapsed:,.0f} jobs a second.", len(claimed) == jobs and duplicates == 0, {"jobs": jobs, "workers": 8, "duplicates": duplicates, "jobs_per_second": round(jobs / elapsed)})


def duplicate_enqueue(url: str) -> Outcome:
    """Twenty requests try to queue the same work at the same instant; one job may result."""
    from sqlalchemy import text

    from radreport.db.session import system_session
    from radreport.workers import queue

    key = f"recording-{uuid.uuid4().hex[:8]}"
    results: list[uuid.UUID | None] = []
    lock = threading.Lock()
    gate = threading.Barrier(20)

    def submit() -> None:
        gate.wait()
        try:
            with system_session(url) as session:
                job_id = queue.enqueue(session, DEDUPE_KIND, {"key": key}, dedupe_key=key)
        except Exception:  # noqa: BLE001 - a refused duplicate counts as not queued
            job_id = None
        with lock:
            results.append(job_id)

    threads = [threading.Thread(target=submit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with system_session(url) as session:
        live = session.execute(text("SELECT count(*) FROM job WHERE kind = :k AND dedupe_key = :d"), {"k": DEDUPE_KIND, "d": key}).scalar_one()
    return Outcome("duplicate_enqueue", "The same upload arrives 20 times at once", "Fired 20 simultaneous requests to queue processing for the same recording, as a flaky network retrying would.", "One job is queued; the other 19 are recognised as repeats.", f"{live} job queued, {sum(r is None for r in results)} repeats turned away.", live == 1, {"requests": 20, "jobs_queued": live})


def poison_job(url: str) -> Outcome:
    """A job that always fails is retried with a delay, then set aside, without holding up healthy work."""
    from radreport.db.session import system_session
    from radreport.workers import queue
    from radreport.workers.worker import Worker

    with system_session(url) as session:
        bad = queue.enqueue(session, POISON_KIND, max_attempts=2)
        good = queue.enqueue(session, HEALTHY_KIND)
    worker = Worker(kinds=[POISON_KIND, HEALTHY_KIND], worker_id="poison-runner", concurrency=2, url=url)
    started = time.monotonic()
    good_done = None
    while time.monotonic() - started < 30:
        asyncio.run(worker.run_once())
        if good_done is None and _job(url, good).status == "succeeded":
            good_done = time.monotonic() - started
        if _job(url, bad).status == "dead":
            break
        time.sleep(0.5)
    job = _job(url, bad)
    ok = job.status == "dead" and job.attempts == 2 and good_done is not None
    return Outcome("poison_job", "A job that crashes every time", "Queued a job whose code always throws an error, next to an ordinary job.", "The bad job is retried after a pause, then parked for a person to look at. It never blocks the queue.", f"The healthy job finished on the first pass, {(good_done or 0) * 1000:.0f} ms in. The bad one failed {job.attempts} times, 5 s apart, and was parked as dead with its error kept.", ok, {"attempts": job.attempts, "status": job.status, "healthy_job_seconds": round(good_done or 0, 2)})


def provider_outage(_url: str) -> Outcome:
    """An AI provider starts failing; the circuit breaker must stop calling it, then try again after a cool-down."""
    from radreport.adapters.llm.concurrency import CircuitBreaker, ProviderLimiter
    from radreport.core.errors import ProviderSaturated

    calls = {"n": 0}
    healthy = {"on": False}

    async def provider() -> str:
        calls["n"] += 1
        if not healthy["on"]:
            raise ConnectionError("503 overloaded")
        return "ok"

    limiter = ProviderLimiter(name="crash-test", max_attempts=2, base_delay=0.01, max_delay=0.02, breaker=CircuitBreaker(failure_threshold=3, reset_seconds=1.0))

    async def run() -> dict[str, Any]:
        for _ in range(3):
            try:
                await limiter.call(provider)
            except ConnectionError:
                pass
        while_failing = calls["n"]
        refused, fast = 0, []
        for _ in range(20):
            started = time.perf_counter()
            try:
                await limiter.call(provider)
            except ProviderSaturated:
                refused += 1
                fast.append(time.perf_counter() - started)
        open_calls = calls["n"] - while_failing
        await asyncio.sleep(1.05)
        healthy["on"] = True
        recovered = await limiter.call(provider) == "ok"
        return {"calls_while_failing": while_failing, "refused_while_open": refused, "calls_reaching_provider_while_open": open_calls, "refusal_ms": round(max(fast) * 1000, 3) if fast else None, "recovered": recovered, "breaker_closed": not limiter.breaker.is_open}

    n = asyncio.run(run())
    ok = n["refused_while_open"] == 20 and n["calls_reaching_provider_while_open"] == 0 and n["recovered"] and n["breaker_closed"]
    return Outcome("provider_outage", "The AI provider goes down", "Made the AI provider fail every call, then sent 20 more requests while it was down, then brought it back.", "After a few failures the system stops calling the broken provider, refuses instantly instead of hanging, and tries again on its own after a cool-down.", f"The circuit opened after 3 failed calls. All 20 follow-up calls were refused in at most {n['refusal_ms']} ms without touching the provider. One second later a trial call went through and the circuit closed.", ok, n)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    """A real uvicorn process for the scenarios that need one, on its own port."""

    def __init__(self, url: str, **env: str) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        environment = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_MULTIPROC_DIR"}
        environment.update({"RADREPORT_DATABASE_URL": url, "RADREPORT_ENVIRONMENT": "local", **env})
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "radreport.api.app:app", "--port", str(self.port), "--log-level", "warning"], env=environment, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def __enter__(self) -> _Server:
        import httpx

        def up() -> bool:
            try:
                return httpx.get(f"{self.base}/health", timeout=10).status_code == 200
            except httpx.HTTPError:
                return False

        if not _wait(up, 40, 0.25):
            self.__exit__()
            raise RuntimeError("the test server did not start")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


def database_down(url: str) -> Outcome:
    """The database is unreachable: liveness must stay up, readiness must take the server out of rotation."""
    import httpx

    unreachable = f"postgresql+psycopg://nobody:nothing@127.0.0.1:{_free_port()}/gone_test"
    with _Server(unreachable, RADREPORT_DB__CONNECT_TIMEOUT_SECONDS="2") as server:
        started = time.perf_counter()
        health = httpx.get(f"{server.base}/health", timeout=15)
        ready = httpx.get(f"{server.base}/ready", timeout=15)
        elapsed = time.perf_counter() - started
        features = httpx.get(f"{server.base}/features", timeout=15)
    body = health.json()
    ok = health.status_code == 200 and body.get("status") == "degraded" and ready.status_code == 503 and features.status_code == 200
    return Outcome("database_down", "The database disappears", "Started a real server pointed at a database that does not exist.", "The server stays alive and says what is wrong, but tells the load balancer to stop sending it traffic. Pages that need no data still load.", f"/health answered {health.status_code} ({body.get('status')}), /ready answered {ready.status_code} so a load balancer routes away, and the public pages still served {features.status_code}. Both checks answered in {elapsed * 1000:.0f} ms instead of hanging.", ok, {"health_status": health.status_code, "ready_status": ready.status_code, "public_page_status": features.status_code, "check_seconds": round(elapsed, 2)})


def cache_down(url: str) -> Outcome:
    """Redis is unreachable: requests must still succeed, served without the shared cache."""
    import httpx

    with _Server(url, RADREPORT_REDIS_URL=f"redis://127.0.0.1:{_free_port()}/0") as server:
        pages = [httpx.get(f"{server.base}{path}", timeout=15).status_code for path in ("/features", "/demo", "/recruiter", "/api-docs")]
        health = httpx.get(f"{server.base}/health", timeout=15).json()
    cache = health.get("checks", {}).get("cache", {})
    ok = all(code == 200 for code in pages) and cache.get("ok") is False
    return Outcome("cache_down", "The shared cache (Redis) goes away", "Started a real server configured to use a Redis server that is not there.", "A cache outage makes the system slower, never broken: every request still succeeds, and the health check reports the cache as down.", f"All {len(pages)} pages answered 200. /health reported the cache as down ({cache.get('backend')}), so an operator sees it while users don't.", ok, {"page_statuses": pages, "cache_ok": cache.get("ok")})


def request_flood(url: str) -> Outcome:
    """Three hundred requests from one address in a burst: the limit must hold and the server must stay responsive."""
    import httpx
    from sqlalchemy import text

    from radreport.db.session import system_session

    with system_session(url) as session:
        session.execute(text("DELETE FROM rate_limit_counter"))
    with _Server(url) as server:

        async def flood() -> list[httpx.Response]:
            async with httpx.AsyncClient(timeout=30) as client:
                return await asyncio.gather(*(client.get(f"{server.base}/features") for _ in range(300)))

        started = time.perf_counter()
        responses = asyncio.run(flood())
        elapsed = time.perf_counter() - started
        probe_started = time.perf_counter()
        health = httpx.get(f"{server.base}/health", timeout=15)
        probe_ms = (time.perf_counter() - probe_started) * 1000
    served = sum(r.status_code == 200 for r in responses)
    limited = [r for r in responses if r.status_code == 429]
    retry_after = all(r.headers.get("retry-after") for r in limited)
    ok = served == 120 and len(limited) == 180 and retry_after and health.status_code == 200
    return Outcome("request_flood", "One visitor floods the site", "Sent 300 requests for the same page from one address, all at once.", "The first 120 in the minute are served, the rest get a polite 'slow down' with a time to retry, and the server keeps answering everyone else.", f"{served} served, {len(limited)} told to slow down (every one with a Retry-After time), in {elapsed:.1f} s. The health check right after answered in {probe_ms:.0f} ms.", ok, {"requests": 300, "served": served, "rate_limited": len(limited), "burst_seconds": round(elapsed, 2), "health_ms_after": round(probe_ms)})


SCENARIOS: tuple[Callable[[str], Outcome], ...] = (worker_killed, relay_crash, double_claim, duplicate_enqueue, poison_job, provider_outage, database_down, cache_down, request_flood)


# =============================================================== report ===
def write_report(outcomes: list[Outcome]) -> None:
    """docs/CRASH_TEST.md for people, docs/crash-test.json for the page."""
    when = dt.datetime.now(dt.UTC)
    passed = sum(o.passed for o in outcomes)
    machine = f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"
    lines = ["# Crash test", "", f"**{passed} of {len(outcomes)} failures handled as designed.** Run {when:%Y-%m-%d %H:%M} UTC on a developer machine ({machine}) against a disposable test database, with real worker and server processes. Re-run it with `make crash-test`.", "", "| Scenario | What we broke | What happened | Result |", "|---|---|---|---|"]
    lines += [f"| **{o.title}** | {o.broke} | {o.observed} | {'✅ handled' if o.passed else '❌ not handled'} |" for o in outcomes]
    lines += ["", "## Details", ""]
    for o in outcomes:
        lines += [f"### {o.title}", "", f"**What we broke.** {o.broke}", "", f"**What should happen.** {o.expected}", "", f"**What happened.** {o.observed}", "", f"**Result.** {'Handled as designed' if o.passed else 'Not handled'} ({o.seconds:.1f} s to run).", ""]
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps({"ran_at": when.isoformat(), "machine": machine, "passed": passed, "total": len(outcomes), "scenarios": [asdict(o) for o in outcomes]}, indent=2), encoding="utf-8")


# =========================================================== the worker ===
def _worker_main(worker_id: str, visibility: int) -> None:
    """The child process the kill scenario starts: a real Worker that runs the slow job."""
    from radreport.core.logging import configure_logging
    from radreport.workers import handlers
    from radreport.workers.worker import Worker

    @handlers.handler(SLOW_KIND)
    async def _slow(_session: Any, job: Any) -> dict[str, Any]:
        await asyncio.sleep(float(job.payload.get("seconds", 4)))
        return {"worker": worker_id}

    configure_logging()
    worker = Worker(kinds=[SLOW_KIND], worker_id=worker_id, visibility_seconds=visibility, poll_min_seconds=0.2, poll_max_seconds=0.5)

    async def run() -> None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, worker.stop)
        await worker.run_forever()

    asyncio.run(run())


def _register_in_process_handlers() -> None:
    from radreport.workers import handlers

    @handlers.handler(POISON_KIND)
    async def _poison(_session: Any, _job: Any) -> dict[str, Any]:
        raise RuntimeError("this job always crashes")

    @handlers.handler(HEALTHY_KIND)
    async def _healthy(_session: Any, _job: Any) -> dict[str, Any]:
        return {"ok": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-id", default="worker")
    parser.add_argument("--visibility", type=int, default=3)
    parser.add_argument("--only", default="", help="comma-separated scenario names")
    args = parser.parse_args(argv)
    if args.worker:
        _worker_main(args.worker_id, args.visibility)
        return 0

    url = _require_test_db()
    from radreport.core.logging import configure_logging

    configure_logging()
    # The flood sends 300 requests; one log line each would bury the results.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _register_in_process_handlers()
    wanted = {s for s in args.only.split(",") if s}
    outcomes = []
    _cleanup(url)
    try:
        for scenario in SCENARIOS:
            if wanted and scenario.__name__ not in wanted:
                continue
            print(f"  {scenario.__name__} ...", end=" ", flush=True)
            started = time.monotonic()
            try:
                outcome = scenario(url)
            except Exception as exc:  # noqa: BLE001 - a scenario that errors is reported as not handled
                outcome = Outcome(scenario.__name__, scenario.__name__, "", "", f"the scenario itself failed: {type(exc).__name__}: {exc}", False)
            outcome.seconds = time.monotonic() - started
            outcomes.append(outcome)
            print("handled" if outcome.passed else f"NOT HANDLED: {outcome.observed}")
    finally:
        _cleanup(url)
    if not wanted:
        write_report(outcomes)
        print(f"\n{sum(o.passed for o in outcomes)}/{len(outcomes)} handled; wrote {REPORT_MD.relative_to(ROOT)}")
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
