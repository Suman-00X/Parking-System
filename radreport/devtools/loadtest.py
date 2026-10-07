"""Puts a running server under concurrent load and reports throughput, latency, statements per request and database connections.

Order: refuse outside developer machines (require_local) -> give every virtual user its own lab
account and token, so per-user rate limits apply exactly as in production (_virtual_users) ->
run the users concurrently against a read-heavy mix of lab and probe routes (_user) while sampling
the server's connections to Postgres (_sample_connections) -> print the report (main).

    python -m radreport.devtools.loadtest --base-url http://127.0.0.1:8078 --users 500 --seconds 60 --lab sunrise
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select, text

from radreport.auth.lab import issue_access_token
from radreport.core.types import UserRole
from radreport.db.models.identity import AppUser
from radreport.db.models.tenancy import Tenant
from radreport.db.session import get_engine, system_session, tenant_session
from radreport.devtools.local_accounts import require_local

#: (weight, path): what a reviewing lab mostly does — look at the queue, page through recordings — plus the probes a load balancer sends.
MIX: tuple[tuple[int, str], ...] = ((5, "/review/queue"), (3, "/review/queue/stats"), (3, "/ingest/recordings?page_size=20"), (1, "/review/metrics/usefulness"), (1, "/health"))


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter[int] = field(default_factory=Counter)
    statements: list[int] = field(default_factory=list)
    connections: list[int] = field(default_factory=list)


def _virtual_users(lab_slug: str, count: int) -> list[str]:
    """Bearer tokens for `count` load-test radiologists in the lab, created on first use."""
    with system_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.slug == lab_slug)).scalar_one_or_none()
        if tenant is None:
            raise SystemExit(f"no lab {lab_slug!r}; run make seed-local first")
        tenant_id = tenant.id
    with tenant_session(tenant_id) as session:
        existing = list(session.execute(select(AppUser).where(AppUser.tenant_id == tenant_id, AppUser.employee_code.like("LOAD-%"))).scalars())
        for n in range(len(existing), count):
            session.add(AppUser(tenant_id=tenant_id, employee_code=f"LOAD-{n:04d}-{uuid.uuid4().hex[:4]}", display_name=f"Load User {n}", roles=[UserRole.RADIOLOGIST]))
        session.flush()
        users = list(session.execute(select(AppUser).where(AppUser.tenant_id == tenant_id, AppUser.employee_code.like("LOAD-%")).limit(count)).scalars())
        return [issue_access_token(user_id=u.id, tenant_id=tenant_id, roles=[UserRole.RADIOLOGIST])[0] for u in users]


async def _user(client: httpx.AsyncClient, token: str, deadline: float, results: Results, think: float) -> None:
    paths = [p for w, p in MIX for _ in range(w)]
    headers = {"Authorization": f"Bearer {token}"}
    while time.monotonic() < deadline:
        started = time.perf_counter()
        try:
            response = await client.get(random.choice(paths), headers=headers)
            results.statuses[response.status_code] += 1
            if "x-query-count" in response.headers:
                results.statements.append(int(response.headers["x-query-count"]))
        except httpx.HTTPError:
            results.statuses[0] += 1
        results.latencies_ms.append((time.perf_counter() - started) * 1000)
        await asyncio.sleep(think * random.uniform(0.5, 1.5))


async def _sample_connections(deadline: float, results: Results) -> None:
    while time.monotonic() < deadline:
        with get_engine().connect() as conn:
            results.connections.append(int(conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND backend_type = 'client backend'")).scalar_one()))
        await asyncio.sleep(1)


def _pct(values: list[float], q: float) -> float:
    return statistics.quantiles(values, n=100)[int(q) - 1] if len(values) > 1 else (values[0] if values else 0.0)


async def run(base_url: str, tokens: list[str], seconds: float, think: float) -> Results:
    results = Results()
    deadline = time.monotonic() + seconds
    limits = httpx.Limits(max_connections=len(tokens) + 10, max_keepalive_connections=len(tokens) + 10)
    async with httpx.AsyncClient(base_url=base_url, timeout=30, limits=limits) as client:
        await asyncio.gather(_sample_connections(deadline, results), *(_user(client, t, deadline, results, think) for t in tokens))
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=500)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--think", type=float, default=1.0, help="mean seconds each user waits between requests")
    parser.add_argument("--lab", default="sunrise")
    args = parser.parse_args(argv)
    require_local()
    tokens = _virtual_users(args.lab, args.users)
    results = asyncio.run(run(args.base_url, tokens, args.seconds, args.think))
    total = sum(results.statuses.values())
    ok = sum(n for code, n in results.statuses.items() if 200 <= code < 400)
    print(f"users {len(tokens)}  duration {args.seconds:.0f}s  requests {total}  throughput {total / args.seconds:.1f} req/s")
    print(f"success {ok / total:.2%}  statuses {dict(sorted(results.statuses.items()))}")
    print(f"latency ms  p50 {_pct(results.latencies_ms, 50):.1f}  p95 {_pct(results.latencies_ms, 95):.1f}  p99 {_pct(results.latencies_ms, 99):.1f}")
    if results.statements:
        print(f"statements/request  mean {statistics.fmean(results.statements):.1f}  p95 {_pct([float(s) for s in results.statements], 95):.0f}  max {max(results.statements)}")
    if results.connections:
        print(f"postgres client connections  mean {statistics.fmean(results.connections):.0f}  max {max(results.connections)}")


if __name__ == "__main__":
    main()
