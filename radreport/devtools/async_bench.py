"""Measures what the async driver gains: the same paged read served by a sync route and an async route, under the same load.

Order: refuse outside developer machines (require_local) -> start a bare app with one sync and one
async route that run the same query, each on the session layer the real routes use (_app) -> put
each route under the same concurrent load in turn (_load) -> print requests per second and latency
for both, and the ratio (main).

    python -m radreport.devtools.async_bench --clients 200 --seconds 15 --sleep-ms 20

`--sleep-ms` adds `pg_sleep` to each query, standing in for a slow statement: that is where a
sync route holds a worker thread for the whole wait and an async route does not.
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import statistics
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio.to_thread
import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select, text

from radreport.api.pagination import Page, paginate, paginate_async
from radreport.core.config import get_settings
from radreport.db.async_session import async_read_session
from radreport.db.models.tenancy import Tenant
from radreport.db.session import read_session
from radreport.devtools.local_accounts import require_local


@dataclass
class Run:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0


def _app(sleep_ms: int) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The same threadpool size the real app runs sync routes on.
        anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().db.threadpool_size
        yield

    app = FastAPI(lifespan=lifespan)
    query = select(Tenant).order_by(Tenant.name, Tenant.id)
    pause = text("SELECT pg_sleep(:s)").bindparams(s=sleep_ms / 1000.0)

    @app.get("/sync")
    def sync_route() -> dict[str, int]:
        with read_session() as session:
            if sleep_ms:
                session.execute(pause)
            paged = paginate(session, query, Page.of(1, 20))
        return {"rows": len(paged.rows)}

    @app.get("/async")
    async def async_route() -> dict[str, int]:
        async with async_read_session() as session:
            if sleep_ms:
                await session.execute(pause)
            paged = await paginate_async(session, query, Page.of(1, 20))
        return {"rows": len(paged.rows)}

    return app


def _run_server(sleep_ms: int, port: int) -> None:
    uvicorn.run(_app(sleep_ms), host="127.0.0.1", port=port, log_level="warning")


def _serve(sleep_ms: int, port: int) -> multiprocessing.Process:
    """The app in its own process, so the load generator does not share its interpreter lock."""
    server = multiprocessing.Process(target=_run_server, args=(sleep_ms, port), daemon=True)
    server.start()
    for _ in range(200):
        try:
            httpx.get(f"http://127.0.0.1:{port}/docs", timeout=1)
            return server
        except httpx.HTTPError:
            time.sleep(0.05)
    server.terminate()
    raise SystemExit("the benchmark server did not start")


async def _load(base_url: str, path: str, clients: int, seconds: float) -> Run:
    run = Run()
    deadline = time.monotonic() + seconds
    limits = httpx.Limits(max_connections=clients, max_keepalive_connections=clients)

    async def client_loop(client: httpx.AsyncClient) -> None:
        while time.monotonic() < deadline:
            started = time.perf_counter()
            try:
                response = await client.get(path)
                ok = response.status_code == 200
            except httpx.HTTPError:
                ok = False
            if ok:
                run.latencies_ms.append((time.perf_counter() - started) * 1000.0)
            else:
                run.errors += 1

    async with httpx.AsyncClient(base_url=base_url, timeout=60, limits=limits) as client:
        await client.get(path)  # warm the pools before timing
        await asyncio.gather(*(client_loop(client) for _ in range(clients)))
    return run


def _pct(values: list[float], q: int) -> float:
    return statistics.quantiles(values, n=100)[q - 1] if len(values) > 1 else (values[0] if values else 0.0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clients", type=int, default=200)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--sleep-ms", type=int, default=0, help="pg_sleep added to every query, to stand in for a slow statement")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args(argv)
    require_local()
    server = _serve(args.sleep_ms, args.port)
    base = f"http://127.0.0.1:{args.port}"
    db = get_settings().db
    print(f"clients {args.clients}  seconds {args.seconds:.0f}  pg_sleep {args.sleep_ms} ms  pools {db.pool_size}+{db.max_overflow} each, sync on {db.threadpool_size} threads")
    rates: dict[str, float] = {}
    for path in ("/sync", "/async"):
        run = asyncio.run(_load(base, path, args.clients, args.seconds))
        rates[path] = len(run.latencies_ms) / args.seconds
        print(f"{path:7} {rates[path]:8.1f} req/s  p50 {_pct(run.latencies_ms, 50):7.1f} ms  p95 {_pct(run.latencies_ms, 95):7.1f} ms  errors {run.errors}")
    server.terminate()
    if rates["/sync"]:
        print(f"async / sync  {rates['/async'] / rates['/sync']:.2f}x")


if __name__ == "__main__":
    main()
