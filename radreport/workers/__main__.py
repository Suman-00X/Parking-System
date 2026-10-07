"""Run a worker: python -m radreport.workers [--kinds run_pipeline,ensure_partitions] [--concurrency 2] [--metrics-port 9101]."""

from __future__ import annotations

import argparse
import asyncio
import signal

from prometheus_client import start_http_server

from radreport.core.logging import configure_logging
from radreport.observability.errors import setup_error_reporting
from radreport.observability.metrics import process_registry
from radreport.observability.tracing import setup_tracing
from radreport.workers import handlers
from radreport.workers.worker import Worker


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Drain the radreport job queue.")
    parser.add_argument("--kinds", default="", help="comma-separated job kinds; default every registered kind")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--visibility-seconds", type=int, default=300, help="lease length; a job whose worker dies is reclaimed after this")
    parser.add_argument("--once", action="store_true", help="claim one batch, run it, and exit")
    parser.add_argument("--metrics-port", type=int, default=0, help="serve Prometheus metrics on this port; 0 serves none")
    args = parser.parse_args(argv)
    configure_logging()
    setup_error_reporting()
    setup_tracing()
    if args.metrics_port:
        # A worker is its own process, so it is scraped on its own port; nothing here is lab data.
        start_http_server(args.metrics_port, registry=process_registry())
    worker = Worker(kinds=[k for k in args.kinds.split(",") if k] or handlers.kinds(), concurrency=args.concurrency, visibility_seconds=args.visibility_seconds)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Finish the jobs in hand, then exit: an interrupted job would wait out its lease before anyone retried it.
            loop.add_signal_handler(sig, worker.stop)
        if args.once:
            await worker.run_once()
        else:
            await worker.run_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
