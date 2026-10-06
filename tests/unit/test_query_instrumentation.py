"""Statement counting and timing: per scope, across worker threads, and the process-wide percentiles."""

from __future__ import annotations

import threading

from radreport.db.instrumentation import QueryMetrics, QueryStats, _percentile, current_stats, query_scope


def test_a_scope_counts_what_runs_inside_it() -> None:
    with query_scope("unit", keep_statements=True) as stats:
        current = current_stats()
        assert current is stats
        current.record(2.0, "SELECT 1", slow=False)
        current.record(150.0, "SELECT pg_sleep(0.15)", slow=True)
    assert (stats.count, stats.slow, stats.total_ms) == (2, 1, 152.0)
    assert stats.statements == ["SELECT 1", "SELECT pg_sleep(0.15)"]
    assert current_stats() is None


def test_a_thread_started_inside_a_scope_counts_against_it() -> None:
    """Sync route handlers run on a worker thread; their statements must still land on the request."""
    import contextvars

    with query_scope("threaded") as stats:
        ctx = contextvars.copy_context()
        worker = threading.Thread(target=ctx.run, args=(lambda: current_stats().record(1.0, "SELECT 1", slow=False),))
        worker.start()
        worker.join()
    assert stats.count == 1


def test_percentiles_and_the_heaviest_routes() -> None:
    metrics = QueryMetrics(window=100)
    for ms in range(1, 101):
        metrics.record_query(float(ms), slow=ms >= 100)
    for count, label in ((3, "light"), (40, "heavy"), (42, "heavy")):
        stats = QueryStats(label=label, count=count)
        metrics.record_scope(stats)
    snap = metrics.snapshot()
    assert snap["queries_total"] == 100
    assert snap["slow_queries_total"] == 1
    assert snap["query_ms"]["p50"] in (50.0, 51.0)
    assert snap["query_ms"]["p99"] >= 99.0
    assert snap["routes"][0] == {"route": "heavy", "requests": 2, "avg_queries": 41.0, "max_queries": 42, "avg_db_ms": 0.0}


def test_the_window_is_bounded() -> None:
    metrics = QueryMetrics(window=10)
    for _ in range(50):
        metrics.record_query(1.0, slow=False)
    assert metrics.snapshot()["query_ms"]["sampled"] == 10
    assert _percentile([], 0.5) == 0.0
