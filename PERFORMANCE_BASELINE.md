# Performance baseline and what the optimisation work changed

Measured on 2026-10-07 on a developer laptop (Apple Silicon, 11 cores, Postgres 16 via Homebrew,
`max_connections = 100`), with synthetic data: two labs, ~1,500 pipeline runs with stage traces.
The load generator ran on the same machine as the app and the database, so absolute throughput is
a floor, not a forecast. Everything below can be re-measured with the commands shown.

There is no production database yet, so the "monthly query volume", "DB CPU under peak" and
"current cost" lines of the roadmap could not be measured; they are listed as open at the end.

## Statements per request

From the `x-query-count` header every response carries (local/test environments) and from
`/admin/api/ops/queries`.

| Request | Before | After | Change |
|---|---:|---:|---|
| `GET /admin/labs` | 11 | 6 | one `set_config` round trip per session instead of two; admin session and account read in one join; rate-limit upsert on a bare connection |
| `GET /admin/labs/{id}` | 17 | 10 | as above, plus the lab row read once per request instead of twice |
| `GET /admin/labs/{id}/readiness` | 19 | 13 | gold-set item counts grouped into one query instead of one per set |
| `GET /admin/labs/{id}/onboarding` | 23 | 17 | the overview reuses the readiness checks' counts instead of re-reading them |
| `GET /admin/providers` | 11 | 6 | |
| `GET /admin/users` | 10 | 5 | |
| `GET /admin/account` | 7 | 3 | |
| One pipeline run (15 stages) | 37 | 8 | stage trace and domain rows buffered into one batched flush |
| Loading 5,000 corpus reports | ~5,000 INSERTs | 13 statements, 0.18 s | batched `INSERT`s of 1,000 rows (`db/bulk.py`) |
| Re-mining a lab's lexicon | 1 + 1 per mined term | < 15 | the set's terms read once |
| Roster import, 60 people | 126 | < 20 | everyone read once; new rows go out as one batch |

Pinned by `tests/db/test_query_counts.py`, `test_pipeline_write_batching.py` and
`test_bulk_import.py`: counts must not grow with the number of labs, staff or accounts.

## Statement times under load

One worker behind PgBouncer, 100 concurrent lab users for 30 s (3,030 requests, all 200):

| | p50 | p95 | p99 |
|---|---:|---:|---:|
| statement time (ms) | 0.78 | 3.76 | 4.25 |
| statements per request | 3 | 5 | 5 |
| request latency (ms) | 10.4 | 41.7 | 259.3 |

No statement crossed the 100 ms slow-query threshold. Per route: the review queue averages 3.1
statements and 5.1 ms of database time, the paged recording list 5.0 statements and 5.3 ms.

## Concurrency: 500 users

`python -m radreport.devtools.loadtest --base-url http://127.0.0.1:8078 --users 500 --seconds 40`
— every virtual user is its own lab account, so per-user rate limits apply as in production.

| Setup | Success | Throughput | p50 | p95 |
|---|---:|---:|---:|---:|
| 2 workers, direct to Postgres, pool 30 + 10 each (the Phase 1 pool) | 6.2% | 416 req/s of mostly errors | 176 ms | 621 ms |
| 4 workers via PgBouncer, before the deadlock fix | 8.7% | 26 req/s | 30 s (timeouts) | 30 s |
| 4 workers via PgBouncer, after the fix | **99.99%** | **307 req/s** | **8.6 ms** | 3.2 s |
| 4 workers via PgBouncer, 150 users | 100% | 140 req/s | 8.2 ms | 401 ms |
| 4 workers via PgBouncer, 300 users | 100% | 240 req/s | 6.8 ms | 1.27 s |

What the first two rows found, both fixed:

1. **Raising the pool without PgBouncer exhausts Postgres.** Two workers x (30 + 10 sync + 10 async)
   plus every other client is more than `max_connections = 100`: "too many clients already". The
   app now warns at start-up (`connection_budget_exceeded`) when its pools could exceed the server's
   limit, and the async engine has its own small pool. With PgBouncer, 160 app connections share
   25 server connections (`tests/db/test_pgbouncer.py` pins 200 clients on ≤ 25).
2. **A thread-versus-connection deadlock under overload.** Every threadpool slot ended up held by a
   request waiting for a pooled connection, while the requests holding connections waited for a
   slot to finish on; transactions stayed open until Postgres killed them. Fixed by admitting only
   as many request sessions as the pool has connections (they wait on the event loop, holding no
   thread), closing each session before the response is sent, giving the access middleware its own
   small pool, binding the lab when a transaction starts rather than when a session opens, and a
   larger threadpool. Backstops: `idle_in_transaction_session_timeout = 60s` on the app's login role
   and `idle_transaction_timeout = 60` in PgBouncer. Pinned by `tests/db/test_overload.py`.

At 500 users the p95 is queueing on a laptop that also runs the load generator; p50 stays under
10 ms, which is the sign that the database is not the bottleneck.

## Sync vs async driver

`python -m radreport.devtools.async_bench --clients 200 --seconds 10 [--sleep-ms 20]`. The same
paged read (20 labs and a count) through a sync route on `read_session()` and an async route on
`async_read_session()`, with the default pools (sync 30 + 10 on 100 threads, async 5 + 5). The
server runs in its own process; client and server share one laptop, so read the ratio, not the
absolute numbers. Three runs each:

| Query | Sync | Async | Async / sync | p95 sync → async |
|---|---:|---:|---:|---:|
| fast (no added wait) | 97–197 req/s | 279–412 req/s | 1.4–2.9x | 4.5–5.3 s → 2.0–3.1 s |
| 20 ms (`pg_sleep`) | 88–91 req/s | 382–390 req/s | **4.3–4.4x** | 5.2–5.6 s → 0.6 s |

A sync route holds a worker thread for the whole query; an async one gives the event loop back
while it waits, so the gain grows with statement time. With both pools set to 20 + 10, the 20 ms case fell to about 1.1x on this machine, as the two pools together came
close to Postgres' `max_connections = 100` and requests waited for connections; behind PgBouncer
that limit is the pooler's, not Postgres'.

### The whole app, bridged vs sync

Every sync route now runs bridged (`db/bridge.py`). The same `loadtest` (300 users, 20 s, 0.2 s
think time, one worker, direct to Postgres) against a server built from the commit before the
bridge and one from after it, each run alone:

| Database | Sync (before) | Bridged (after) |
|---|---|---|
| local, sub-millisecond | 97–111 req/s, 100% success, p50 2.2 s | 92–101 req/s, 99.9% success, p50 2.3–2.6 s |
| behind a proxy adding 2 ms per round trip | 91 req/s, **97.97% success** (37 × 500), p50 2.9 s | **95.5 req/s, 100% success**, p50 2.4 s |

On this laptop the process is CPU-bound — the load generator shares the machine and every query
returns in well under a millisecond — so threads were never what limited it, and the greenlet hop
costs a few percent. Once the database is a network hop away, the sync server's threads pile up
waiting and requests fail; the bridged one keeps answering. Statements per request are unchanged
(4.2–4.3). A like-for-like production comparison is still to do.

## Caches

| Cache | Measured |
|---|---|
| Request + shared cache, steady admin traffic | hit rate > 80% (`tests/db/test_shared_cache.py`) |
| Model response cache | a repeated identical request costs $0; k samples stay k distinct answers |

## Still to measure on production

- Query count per request over 100 sampled production requests, and `pg_stat_statements`' top 20
  with `EXPLAIN ANALYZE` on a replica (`python -m radreport.devtools.query_report`).
- DB CPU under real peak load, monthly query volume, and the real monthly cost (the roadmap's
  $10k/month is an assumption).
- The replica share of reads (the code routes them and tags them, `reads_by_target` in
  `/admin/api/ops/queries`; the local test points the "replica" at the primary).
