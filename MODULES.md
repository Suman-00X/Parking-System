# Modules and submodules

A map of `radreport/` for someone who has to change it. Nine broad modules; the
22 Python packages sit inside them. Each module opens with a file table — line
count, path, one line on what it is for — and then describes each submodule and,
in pointers, the work it actually does.

**246 Python files, 32,156 lines.** Counts taken from the filesystem on
2026-10-07. Aggregate tables are at the bottom: [by module](#by-module),
[by category](#by-category), [data](#data), [HTTP](#http-routes),
[UI](#ui) and [tests](#tests).

Grouping is by **when the code runs** and **what it may depend on**, not by
layer. Dependency direction is one-way: Foundation → everything; Engines → the
Pipeline but never back; Governance reads from Onboarding, Pipeline and Review
but is never called by them; Surfaces and the Background work loops call down
and are called by nothing. The two exceptions are deliberate and narrow: any
module may put a job on the queue (`workers/queue.py`) or write a domain event
(`events/outbox.py`) inside its own transaction.

| # | Module | Packages | Runs | Files | Lines |
|---|---|---|---|---:|---:|
| 1 | [Foundation](#1-foundation) | `core/`, `db/`, `cache/`, `devtools/`, `observability/` | always | 90 | 9,922 |
| 2 | [Onboarding](#2-onboarding) | `onboarding/` | once per lab, before clinical traffic | 15 | 3,259 |
| 3 | [Capture](#3-capture) | `ingest/`, `adapters/storage/` | per recording | 6 | 547 |
| 4 | [Pipeline](#4-pipeline) | `pipeline/`, `knowledge/` | per report | 41 | 5,582 |
| 5 | [Engines](#5-engines) | `adapters/llm/`, `adapters/asr/` | called by the pipeline | 16 | 1,542 |
| 6 | [Review and export](#6-review-and-export) | `review/`, `export/` | per draft, then per signature | 10 | 1,301 |
| 7 | [Governance](#7-governance) | `eval/`, `autonomy/`, `adaptation/`, `monitoring/` | out of band | 19 | 2,263 |
| 8 | [Surfaces](#8-surfaces) | `api/`, `admin/`, `auth/` | per HTTP request | 36 | 6,936 |
| 9 | [Background work](#9-background-work) | `workers/`, `events/` | per job, per event | 13 | 804 |

---

## 1. Foundation

The substrate. Tenancy, the schema, configuration, caching, and the primitives
the other eight modules are not allowed to re-invent. Depends on nothing in this
repository, with two exceptions: `cache/filters.py` uses `knowledge/bloom.py`,
and the `devtools/` entry points call into the modules they seed or measure.

**90 files, 9,922 lines.** (6 package `__init__` stubs omitted below; the 24
migration revisions are listed in their own table further down.)

| Lines | File | Purpose |
|---:|---|---|
| 541 | `core/types.py` | Every enum and value set. The migration builds its CHECK constraints from these. |
| 233 | `db/models/onboarding.py` | §6.10 S0–S7: import batches, artifacts, template candidates, merge proposals, corpus, collisions, readiness checks |
| 211 | `db/models/reporting.py` | §6.6 drafts, field values, provenance spans, verification findings, critical rules and alerts, autonomy observations |
| 280 | `db/models/knowledge.py` | §6.5 templates, versions, fields, lexicon sets/terms/variants, potential terms and the watcher's watermark, autonomy classes, speaker bias |
| 214 | `core/tenancy.py` | §11.3 exception lists, tenant scope, lifecycle transition gates |
| 159 | `devtools/synthetic.py` | Synthetic audio, dictations, patients, `.docx` templates — no PHI on dev machines |
| 121 | `db/models/modelconfig.py` | §6.14 providers, model definitions, per-(tenant, task) assignments + append-only log |
| 148 | `db/models/review.py` | §6.7 revisions, edit events, final reports, draft-usefulness reports |
| 141 | `db/models/asr.py` | §6.4 ASR runs, segments, transcripts, labelled utterances |
| 130 | `db/models/tenancy.py` | `tenant`, `platform_user`, `admin_session`, shared rate-limit counter, training-consent event log, branding |
| 115 | `db/models/evaluation.py` | §6.9 eval sets, items, runs, results |
| 111 | `db/models/adaptation.py` | §6.12 verbatim transcripts, training corpus snapshots, adaptation runs |
| 139 | `db/models/identity.py` | §6.2 app users, lab refresh tokens, radiologist profiles, patients, studies |
| 265 | `db/session.py` | Engine, `tenant_session()`, `system_session()`, `read_session()` on the replica, `bind_tenant()` — the RLS GUC binding |
| 124 | `db/models/orchestration.py` | §6.8 pipeline runs, stage executions, audit log |
| 167 | `devtools/seed.py` | Seeds the global model catalog, the first product admin, the demo logins and an optional demo lab |
| 126 | `db/introspect.py` | Derives the tenancy facts that migration 0002 and the tests both read |
| 84 | `db/base.py` | Declarative base, `tenant_fk()` composite FKs, tenancy mixins, CHECK builders |
| 122 | `core/errors.py` | Every domain error the system raises deliberately |
| 89 | `db/models/ingestion.py` | §6.3 recordings, with every §9.8 quality measurement |
| 103 | `db/models/__init__.py` | Imports every model so `Base.metadata` is complete for migrations and tests |
| 239 | `core/config.py` | Settings from env and `.env`, `RADREPORT_`-prefixed; `.env` is also copied into the environment for the unprefixed keys |
| 56 | `core/text.py` | Sentence splitting that does not cut decimals ("3.2 cm") in half |
| 67 | `db/bootstrap.py` | Creates the least-privileged app and audit roles (non-owner, so RLS is not bypassed) |
| 50 | `db/migrations/env.py` | Alembic environment |
| 48 | `core/hashing.py` | Content hashing — the idempotency backbone |
| 38 | `core/logging.py` | structlog configuration |
| 146 | `observability/metrics.py` | Prometheus metrics for the web app and the workers, with no lab or patient detail in any label |
| 99 | `observability/tracing.py` | OpenTelemetry tracing, on only when an OTLP endpoint is configured; a span helper that costs nothing when off |
| 57 | `observability/errors.py` | Error reporting to Sentry, on only when a DSN is configured, stripped of anything that could hold patient data |
| 233 | `db/instrumentation.py` | Counts and times every SQL statement, per request and for the whole process, and logs the slow ones |
| 217 | `db/sharding.py` | Places each lab on one of several databases, and keeps the lab's own row present on the database that holds its data |
| 212 | `cache/shared.py` | A cache shared by every request and, with Redis, by every worker and instance; in memory when Redis is not configured |
| 205 | `core/system_config.py` | Operational thresholds ops can change without a release: a typed registry and how a value is resolved |
| 149 | `devtools/local_accounts.py` | Creates a working sign-in for every role on a developer machine, and writes them to a local file |
| 148 | `devtools/query_report.py` | Finds the statements that cost the most and the indexes that are missing, from the database's own statistics |
| 129 | `devtools/template_eval.py` | Measures how many of a template's fields the parser finds alone, and with the template model behind it |
| 119 | `devtools/loadtest.py` | Puts a running server under concurrent load and reports throughput, latency, statements per request and connections |
| 501 | `devtools/crash_test.py` | Breaks the system on purpose against a disposable test database and records what it did, for the features page's crash-test tab |
| 366 | `devtools/demo_lab.py` | Fills a lab with demo data through the product's own HTTP API, as an admin, radiologists and a transcriptionist would |
| 163 | `devtools/record_gifs.py` | Records short GIFs of the running app for the features page and the README, by driving a real browser |
| 144 | `devtools/async_bench.py` | The same paged read through a sync route and an async route under the same load, to measure what the async driver gains |
| 107 | `cache/request.py` | A cache that lives for one HTTP request, so a lookup repeated inside it reaches the database once |
| 97 | `devtools/cost_history.py` | Writes a synthetic history of pipeline runs and stage costs for one lab, so the cost dashboard has something to show |
| 118 | `db/async_session.py` | The async counterpart of `db/session.py`: the same lab binding and pool settings, on an asyncio driver |
| 65 | `db/bridge.py` | Runs sync request code on the event loop with every query on the async driver; `offload` moves blocking work that touches no session to a thread |
| 95 | `cache/filters.py` | Per-lab Bloom filters that let common lookups skip the database when the answer is a definite "no" |
| 95 | `cache/lookups.py` | The lookups worth caching, returned as frozen snapshots rather than ORM rows |
| 72 | `db/table_health.py` | Dead rows waiting for vacuum, when vacuum last ran, and how much space each table takes |
| 80 | `db/pgbouncer_stats.py` | PgBouncer's own view of its pools, read from its admin console, for the pool dashboard |
| 65 | `db/shards.py` | Operating the shards: migrate every one, see where labs live, plan what adding a shard would move, pin a lab |
| 52 | `db/models/events.py` | The outbox written alongside each change, and what each consumer has applied |
| 51 | `db/models/jobs.py` | The job queue workers claim from |
| 45 | `db/models/ops.py` | Tables ops change without a code release: `system_config` and `lab_shard` |
| 40 | `devtools/training_export.py` | Exports radiologists' approvals from the labs given, for fine-tuning the template model |
| 36 | `cache/keys.py` | Cache keys that always carry the lab they belong to, so a cached value cannot be served to another lab |
| 35 | `devtools/storage_report.py` | Prints table health and what compression saves; run as the database owner so every row is counted |
| 35 | `db/models/llm_cache.py` | Stored model responses, so a repeat of an identical request is answered without a call |
| 31 | `db/bulk.py` | Writes many rows in a few statements, for imports where one INSERT per row is the bottleneck |

`devtools/data/template_eval/` holds the fixtures `template_eval.py` scores: eight
template documents as text and `gold.json` with the fields each should yield.

### `core/config.py` — settings
Typed settings objects for the database (pool, replica, shards), events, storage,
audio gates, LLM, ASR, observability and lab sign-in, loaded once and cached.

- Resolves environment into `Settings` with per-concern sub-objects
- Holds the audio-gate thresholds ingest reads
- Single source for the DB URL the app, tests and Alembic all use
- Model identifiers are pinned per-tenant in `model_definition` (§6.14), not here

### `core/types.py` — shared domain types
Every enumerated value set in the system, as tuples. The migration builds its
Postgres `CHECK` constraints from these same tuples, so the Python and the
database cannot drift apart.

- Tenant lifecycle, user and platform roles, consent events
- Audio, ASR and transcript enums; utterance labels and label sources
- Draft, assertion, laterality, fill-source, severity and verdict sets

### `core/errors.py` — domain errors
One hierarchy for everything the domain can refuse, deliberately free of HTTP
concepts. The API layer maps them onto status codes at the boundary.

- Tenancy failures: `NoTenantContext`, `CrossTenantAccess`
- Ingest, model-resolution, stage, budget and provider failures
- Onboarding gates: `ApprovalRequired`, `ConsentRequired`, `BatchBlocked`

### `core/tenancy.py` — tenant scoping
Holds the invariant that no query runs without a tenant filter, for lab users
and product admins alike. A product admin gets the same policy shape as a lab
user and only changes which tenant they are bound to.

- `tenant_scope()` / `system_scope()` context managers around the current principal
- `UNTENANTED_TABLES` / `NULLABLE_TENANT_TABLES` — the §11.3 exception list the build checks against
- `assert_transition_allowed()` — refuses an un-audited tenant switch mid-request

### `core/hashing.py` — content hashing
The idempotency backbone. The same bytes produce the same key, so a retried
upload is a no-op rather than a second report.

- `hash_bytes` / `hash_stream` / `hash_file` for recordings and import artifacts
- `canonical_json` + `hash_config` for `asr_run.config_hash` — same audio, same config, same run
- Backs `content_hash` on recordings, artifacts, final reports and corpus snapshots

### `core/logging.py` — structured logging
Structured log configuration with one hard rule: PHI never enters a log line.
Patient names stay in `patient.name_enc`; logs carry `patient.pseudonym`.

- `configure_logging()` at process start, `get_logger()` everywhere else
- Keeps the pseudonym the prompts already use as the logged identifier

### `core/text.py` — clinical sentence segmentation
Split out of four modules that each had their own `re.compile(r"[.;\n]+")`,
which cuts "a 3.2 cm cyst" in half. Measurement-aware segmentation, shared.

- `split_sentences()` and `iter_sentences_with_offsets()` — offsets kept so provenance survives
- Used by critical-findings scanning, boilerplate mining, corpus mapping and the verifier

### `db/base.py` — declarative base and tenancy mixins
Where the composite-foreign-key rule lives. With `tenant_id` on every table you
can still have `report_draft.tenant_id = A` pointing at `recording.tenant_id =
B` — both rows pass their own RLS policy, and the leak is silent.

- `TenantScoped` / `TenantOptional` mixins; `uuid_pk`, timestamps, soft delete
- `tenant_fk()` — use instead of a bare `ForeignKey` between two tenant-scoped tables
- `enum_check` / `array_enum_check` — constraints generated from `core/types.py`

### `db/session.py` — engine, sessions, RLS binding
`tenant_session()` is the only sanctioned way to open a unit of work against
tenant-scoped tables. It issues `SET LOCAL app.current_tenant_id` inside the
transaction so the database refuses out-of-tenant rows on its own.

- `tenant_session()` / `system_session()` and the engine/sessionmaker singletons;
  `engine_options()` sizes the pool from settings and `connection_budget()` warns
  when the pools outgrow the server's `max_connections`
- The binding is lazy: `_bind_scope()` records the lab on the session, and an
  `after_begin` listener (`_apply_scope()`) issues it as the first statement of
  every transaction, so a session that commits and goes on reading is still bound
- `read_session()` — read-only (`SET TRANSACTION READ ONLY`) and routed to the
  replica when one is configured, has caught up (`replica_lag_seconds()`) and this
  browser has not just written (`replica_url_for_reads()`); otherwise the primary
- `side_session()` — a small separate pool for the access middleware's rate-limit and sign-in checks
- `bind_tenant()` and `select_org()` for an admin acting on one lab — `select_org()` writes the `admin_org_selected` audit row
- `set_config(..., true)` is transaction-local, so a pooled connection cannot leak a binding

### `db/async_session.py` — the async sessions
The same binding and pool settings on psycopg 3's asyncio driver. Its engine is
the one every bridged request uses (see `db/bridge.py` below), sized by the same
`pool_size` and `max_overflow` as the sync engine.

- `get_async_engine()`: one engine per URL per event loop; engines left by a closed loop are disposed when the next one is made
- `async_tenant_session()`, `async_system_session()`, `async_read_session()` for code written as `async def`: the health probe, the recording list and the admin lab and user lists; `async_read_session()` follows the same replica and shard rule as `read_session()`

### `db/bridge.py` — sync request code on the async driver
Every sync route runs inside a SQLAlchemy greenlet on the event loop, the
mechanism `AsyncSession` itself is built on: the ORM code stays synchronous and
each wait on the database becomes an await, so a request holds no worker thread.

- `bridged()` wraps a sync route (`api/routing.py` applies it to every router); `run()` calls sync code bridged from async code, as the access middleware does
- `in_bridge()`: inside it `get_engine`, `get_side_engine` and `get_sessionmaker` hand out the async engine's sync face, so every session factory is bridged without being told
- `offload()`: blocking work that touches no session goes to a thread for its duration — S3 and local-disk objects, Redis, RadLex, Google Translate, scrypt, audio decoding
- `threaded`: marks the routes that run seconds of CPU between queries (onboarding uploads, steps and merge proposals); they keep the sync engine on a worker thread

### `db/instrumentation.py` — statement counting and timing
- `install()` puts timing listeners on every engine; `query_scope()` / `current_stats()` per request or job
- A slow statement (`slow_query_ms`) is logged without its parameters
- `METRICS` — process-wide counts and percentiles, read by `GET /admin/api/ops/queries`;
  `QueryMetricsMiddleware` opens one scope per request

### `db/sharding.py` and `db/shards.py` — labs across databases
Off until `RADREPORT_DB__SHARDS` names more than one database. Then the
directory database holds the platform tables, the lab list and the pins, and each
shard holds a full schema with its labs' rows.

- `HashRing` — consistent hashing with 128 virtual nodes, so adding a shard moves about 1/N of labs
- `pin_lab()` overrides the ring through `lab_shard`; `url_for()` picks the connection for a lab
- `sync_tenant_rows()` copies a changed `tenant` row to the shard that holds the lab; `fan_out()` asks every shard and merges
- `python -m radreport.db.shards migrate | where | plan --add <shard> | pin <lab> <shard>`

### `db/bulk.py` and `db/table_health.py` — bulk writes, table health
- `bulk_insert()` groups rows by the columns they set and sends batches of 1,000; used by the corpus and lexicon imports and by each new lexicon version
- `table_stats()` reads the statistics views (dead rows, last vacuum, size); `BLOAT_RATIO` flags a table
- `compression_ratio()` measures what compression saves on a column — owner only, since it must read every row

### `db/introspect.py` — tenancy facts, derived
Computes the tenancy classification from `Base.metadata` rather than a
hand-written list. The migration and the tests read the same derivation, so a
new table is either classified or a build failure.

- `classify_tables()`, `tenant_scoped_tables()`, `partitioned_tables()`
- `cross_tenant_foreign_keys()` — finds FKs that should be composite and aren't
- `declared_nullable_mismatch()` — model vs. exception-list disagreement

### `db/bootstrap.py` — login roles
Creates the least-privileged login the app and tests use, kept out of the
migration because passwords do not belong in version control.

- `ensure_login_user()` + a `python -m radreport.db.bootstrap` entry point
- Enforces that the app login is neither superuser nor table owner — both bypass RLS

### `db/models/` — the §6 schema (17 files, 68 tables)
One file per §6 area; importing the package registers every table on
`Base.metadata`. A model file not imported here is invisible to both Alembic and
the RLS coverage test.

- `tenancy.py` — tenant lifecycle, platform users, admin sessions, the shared rate-limit counter, consent log
- `identity.py` — app users, lab refresh tokens, radiologist profiles, patients, studies
- `ingestion.py` — `recording`: one audio file, one report
- `asr.py` — ASR runs, segments, transcripts, utterances
- `knowledge.py` — lexicon sets/terms/variants, potential terms and the watch state, templates, versions, fields, speaker bias
- `onboarding.py` — import batches, artifacts, candidates, merge proposals, audit findings
- `reporting.py` — routing decisions, drafts, field values, provenance, verification, alerts
- `review.py` — revisions, edit events, final reports, usefulness reports
- `orchestration.py` — pipeline runs, stage executions, audit log
- `evaluation.py` — eval sets/items/runs/results, with the canonical vs. per-lab split
- `modelconfig.py` — providers, model definitions, per-tenant task assignments and their log
- `adaptation.py` — verbatim transcripts, training-corpus snapshots, adaptation runs
- `jobs.py` — `job`, the queue `workers/` drains
- `events.py` — `outbox_event` and `consumed_event`
- `llm_cache.py` — `llm_response_cache`
- `ops.py` — `system_config` and `lab_shard`

### ⚠️ `db/migrations/versions/` — 23 revisions, 1,683 lines
**Append-only. Never edit an applied migration — add a new one.** Editing one
changes what a deployed database is assumed to contain, which the schema itself
cannot detect. `0001` is generated from `Base.metadata`; every revision after it
is a hand-written diff. Because `0001` builds from the live models, a fresh
database may already hold a later revision's end state, so later revisions check
the database before each step.

`0002` and `0005` need the database **owner** connection (`make migrate-owner`),
because they create roles and grant on new tables. Run as the app role they fail
with a bare permissions error that does not say so. `0007`, `0008`, `0012`–`0015`
and `0017`–`0020` also issue `GRANT`s, and `0013`, `0014` and `0017` hand
functions to the `BYPASSRLS` view-owner role.

| Rev | Lines | File | What it does |
|---|---:|---|---|
| 0001 | 29 | `0001_initial_schema.py` | Creates the whole schema, generated from the model definitions rather than hand-written table by table. |
| 0002 | 136 | `0002_rls_and_roles.py` | Turns on row-level security and splits the database roles, so one lab's rows are unreachable from another lab's connection. **Owner only.** |
| 0003 | 67 | `0003_partitions.py` | Splits the three fastest-growing tables (`asr_segment`, `edit_event`, `audit_log`) into monthly partitions. |
| 0004 | 53 | `0004_template_version_spoken_code.py` | Lets a template keep its spoken code across versions, by scoping the uniqueness rule to the current version only. |
| 0005 | 98 | `0005_admin_auth_and_model_config.py` | Adds admin logins and makes the model configuration writable from the admin panel instead of seed scripts only. **Owner only.** |
| 0006 | 89 | `0006_autonomous_release.py` | Adds the schema that lets an approved class of reports be filed without a human reviewer. |
| 0007 | 79 | `0007_lab_user_auth.py` | Adds lab-user sign-in: a password on each staff account and a per-lab table of refresh tokens. |
| 0008 | 39 | `0008_shared_rate_limits.py` | Adds a request counter every worker shares, for the rate limits that must hold across processes. |
| 0009 | 40 | `0009_batch_platform_submitter.py` | Records which product admin submitted an import batch run from the admin panel. |
| 0010 | 31 | `0010_unlogged_rate_limits.py` | Makes the shared rate-limit counter unlogged, now that every request counts against it. |
| 0011 | 39 | `0011_lookup_indexes.py` | Adds the indexes the query report found missing: foreign keys the hot paths join on, and the cost and per-task metric reads. |
| 0012 | 52 | `0012_system_config.py` | Adds `system_config`: operational thresholds ops can change without a release, platform-wide or per lab. |
| 0013 | 123 | `0013_job_queue.py` | Adds the job queue: a `job` table under row-level security, and `claim_jobs()`, which hands out work with `FOR UPDATE SKIP LOCKED`. |
| 0014 | 102 | `0014_outbox.py` | Adds the transactional outbox: domain events written with each change, relayed once, applied once per consumer. |
| 0015 | 215 | `0015_partitions_v2.py` | Partitions `recording` by lab and `stage_execution` by month, keeps monthly partitions coming, and closes direct access to partitions. |
| 0016 | 112 | `0016_vacuum_and_compression.py` | Tunes autovacuum on the high-churn tables and compresses the large text and JSON columns. |
| 0017 | 93 | `0017_cost_reads_and_eval_view.py` | Adds the cross-lab cost reads behind the cost dashboard, and a materialized copy of the canonical eval set. |
| 0018 | 64 | `0018_llm_response_cache.py` | Adds `llm_response_cache`: stored model replies keyed by lab and an exact request fingerprint. |
| 0019 | 29 | `0019_lab_shard.py` | Adds `lab_shard`: labs pinned to a database shard regardless of the hash ring. |
| 0020 | 73 | `0020_potential_lexicon_terms.py` | Adds `potential_lexicon_term` and `lexicon_watch_state`: phrases radiologists use that the lexicon lacks, and how far the watcher has read. |
| 0021 | 48 | `0021_variant_review.py` | Adds confidence and review status to heard variants, so only confident matches are used unreviewed. |
| 0022 | 44 | `0022_template_parse_task.py` | Allows the `template_parse` task in model assignments and evaluation runs. |
| 0023 | 28 | `0023_template_source_text.py` | Keeps the text of each uploaded template on its import candidate, for training the template model. |
| 0024 | 47 | `0024_backlog_metrics.py` | Adds `work_backlog()`, the job-queue and outbox counts the metrics endpoint reports, owned by the view-owner role so a scrape counts every lab. |

`db/migrations/env.py` (50 lines) is the Alembic environment and is ordinary
code, not history — it is listed in the Foundation table above.

### `core/system_config.py` — settings ops may change
Thresholds that should not need a release to change, held in `system_config`.

- `SETTINGS` declares each one once, with its type, bounds and environment variable
- `resolve()` / `resolve_all()` — a lab's own row, then the platform row, then the environment variable, then the code default
- `set_value()` / `reset_value()` write an audit entry; `SettingRefused` for a bad value
- `adapter_thresholds()` gathers the five numbers the adaptation gates read

### `cache/` — request and shared caches
Lab config, user roles, model assignments and admin sessions are read on almost
every request. Two layers keep that off the database, and every key carries its lab.

- `keys.py` — `key()` is the only way to build a `CacheKey`, and it always takes a lab (or `GLOBAL`)
- `request.py` — `request_cached()` loads once per request; `RequestCacheMiddleware` opens the scope
- `shared.py` — `cached()` reads through the request layer, then `RedisBackend`
  (when `RADREPORT_REDIS_URL` is set) or `MemoryBackend`; `invalidate_after_commit()`
  drops a value only once the change is committed. Values are JSON, never pickles
- `lookups.py` — `tenant_config()` and `user_roles()` as frozen snapshots; `forget_tenant()` / `forget_user()`
- `filters.py` — per-lab Bloom filters (`knowledge/bloom.py`) for "is this audio
  hash already stored" and "is this a term the lab knows". Only a "no" skips the
  database; the unique constraint stays the final word

### `devtools/seed.py` — catalog and demo seed
Seeds the global model catalog (`tenant_id IS NULL`, so a price change touches
one row) and the first product admin, so a fresh database can be signed in to.
`make seed` runs it.

- `seed_model_catalog()` with the corrected $2.00/$10.00 Sonnet 5 pricing
- `seed_platform_admin()` — creates `admin@radreport.local` (or `--admin-email`) as
  `product_admin`, with the password from `RADREPORT_SEED_ADMIN_PASSWORD`; without
  it the account has no password and the seed says how to set one
- `seed_demo_accounts()` — the logins shown on the login page's test-credentials tab, from settings
- `seed_demo_tenant()` behind `--demo-tenant`; creates no model assignment, since that would bypass the eval gate

### `devtools/synthetic.py` — synthetic data
Enforces the Phase 0 rule that no PHI lands on a developer machine. Generates
real FLAC/WAV that passes the ingest gates, plus generated clinical text.

- `synth_audio()` / `synth_lossy_audio()` — valid and deliberately-rejectable files
- `synth_dictation()`, `synth_patient_fields()`, `synth_template_docx()`

### `devtools/` — the other developer and ops tools
Each is a `python -m radreport.devtools.<name>` entry point. `local_accounts`,
`cost_history`, `loadtest` and `async_bench` call `require_local()` first and refuse outside
local, test and development.

- `local_accounts.py` — a sign-in for every platform and lab role, written to a gitignored file (`make seed-local`)
- `cost_history.py` — synthetic runs and stage costs for one lab, marked `trigger=backfill`, with one deliberate spike
- `loadtest.py` — concurrent virtual users, each with its own lab account, against a read-heavy route mix
- `async_bench.py` — one sync and one async route running the same query, served from their own process and loaded in turn; `--sleep-ms` stands in for a slow statement
- `query_report.py` — heaviest statements, unindexed foreign keys, sequential-scan-heavy tables, unused indexes, as markdown
- `storage_report.py` — `db/table_health.py` printed for ops
- `template_eval.py` — scores the template parser, alone and with the template model, against `data/template_eval/`
- `training_export.py` — runs `knowledge/training_data.py` for the labs named, one lab-scoped session each

---

## 2. Onboarding

The S0–S7 onboarding stages: everything that turns a signed contract into a lab the
pipeline can serve. Runs once per lab and must finish before `tenant.status` can
move `onboarding → pilot`.

**15 files, 3,259 lines.** (1 package `__init__` stub omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 421 | `onboarding/templates.py` | S1 — candidates, merge proposals, promotion under R18 |
| 302 | `onboarding/lexicon.py` | S3 — term mining and the blocking collision audit |
| 333 | `onboarding/corpus.py` | S2 — corpus load, derived template map, usage histogram, referrer prior |
| 243 | `onboarding/critical_rules.py` | S6 — seed candidates, author, approve critical-findings rules |
| 281 | `onboarding/paired_audio.py` | S4 — verbatim queue, corpus hours, surface-variant mining |
| 227 | `onboarding/shorthand.py` | S3 — reads a lab's shorthand reference sheet into lexicon terms, so ASR is biased toward the abbreviations said |
| 199 | `onboarding/template_llm.py` | S1 — a small model reads a template the parser was unsure of; only fields the document names are kept |
| 187 | `onboarding/term_watch.py` | Watches what radiologists type for vocabulary the lexicon lacks; approvals become a new lexicon version |
| 186 | `onboarding/boilerplate.py` | S5 — rank normals by corpus share, CSV export |
| 205 | `onboarding/readiness.py` | S7 — the seven checks gating onboarding → pilot |
| 190 | `onboarding/roster.py` | S0 — roster import, voice enrollment, the two separate consents |
| 151 | `onboarding/registration.py` | Lab registration and the tenant lifecycle |
| 219 | `onboarding/template_parse.py` | S1's `.docx` parser, stdlib only. PDF refused, not half-parsed. |
| 114 | `onboarding/batches.py` | The import-batch lifecycle every stage shares |

### `onboarding/batches.py` — the import-batch spine
Every S-stage differs in what it parses and agrees on everything else, and the
"everything else" is here. This is what makes onboarding a module rather than a
pile of import scripts.

- `open_batch()`, `register_artifact()`, `transition()` — the shared lifecycle
- Enforces R18 (nothing reaches `applied` without approval) once, for all stages
- Idempotency by `content_hash`; `revert_batch()` and blocking-issue recounts

### `onboarding/registration.py` — lab lifecycle
Creates the tenant, captures the §10.4 consent columns from the contract, and
owns the status transitions. Moved out of GA because you cannot onboard lab #2
without it.

- `register_lab()` — tenant row + consent state + first lab admin
- `transition_status()` — gated on S7; refuses `onboarding → pilot` with open failures
- `offboard()` for the end of the lifecycle

### `onboarding/roster.py` — S0, roster and voice enrollment
Imports the radiologist roster and captures two genuinely different consents:
one for a voiceprint that identifies the speaker, one for pooled model training.

- `parse_roster_csv()` and `import_roster()`
- `enroll_voice()` — blocked by D18 until enrollment consent is signed
- `record_training_consent()` — separate act, separate record

### `onboarding/templates.py` — S1, import, dedup, promotion
Three human gates, not one: a radiologist signs off per schema, per merge, per
spoken code. Conflating them is how a wrong `spoken_study_code` gets approved
because the field list looked right.

- `submit_templates()` → candidates, never straight into `template_version`
- `build_json_schema()`, `review_candidate()`, `propose_merges()`, `decide_merge()`
- `apply_templates()` / `revert_applied_templates()` — the version history §9.10 rolls back through

### `onboarding/template_parse.py` — S1's document parsers
`.docx` and plain text using the standard library only. A `.docx` is a zip
holding `word/document.xml`, which is enough to recover paragraphs and outline
level without a dependency.

- `extract_paragraphs()`, `parse_template()`, `infer_structure()`
- Refuses PDF explicitly rather than half-parsing it into mangled fields

### `onboarding/template_llm.py` — S1's model fallback
Runs only when the parser was unsure (`needs_fallback()`, against
`templates.llm_fallback_below`) and the lab has a `template_parse` model assigned.

- `fallback_for()` resolves the model; `apply_fallback()` asks it for the fields as JSON
- `fields_from_reply()` drops every field whose label is not in the document (`_grounded()`), so nothing invented reaches a radiologist
- `merge()` — the parser wins where both found a label; any failure leaves the parser's result, with a warning

### `onboarding/corpus.py` — S2, historical report corpus
Signed reports are prose outcomes, not dictations, so nothing is trained here.
What a corpus buys is configuration and measurement: which templates are
actually used, and how often.

- `load_corpus()` with per-record idempotency
- `derive_template_map()` / `verify_mapping()` — machine proposal, human confirmation
- `usage_histogram()`, `refresh_usage_counts()`, `referrer_prior()` for routing priors

### `onboarding/lexicon.py` — S3, mining and collision audit
A blocking stage: every `severity='block'` finding must be resolved before a
batch applies, and S7 re-checks it. Built to be re-run, because the S3↔S4 loop
is designed rather than accidental.

- `mine_terms()` / `run_mining()` over the corpus and declared shorthand
- `get_or_create_tenant_lexicon()`, `record_surface_variants()`
- `run_collision_audit()` + `resolve_finding()` — the phonetic clash gate

### `onboarding/shorthand.py` — S3, shorthand reference sheets
The sheet transcriptionists keep ("LLL = Left Lower Lobe") is the most direct
source of the abbreviations radiologists say. Uploaded from the admin panel.

- `extract_text()` from PDF, Word or text; `find_mappings()` with eight patterns, including two-column tables and slash groups
- `acronym_fit()` scores how well the short form spells the formal one
- `import_shorthand_reference()` writes lexicon terms, each with an audit entry naming the file and line;
  `build_keyterms()` then biases ASR toward them

### `onboarding/term_watch.py` — new vocabulary from live edits
Runs after onboarding, on clinical traffic: the `watch_lexicon` job scans every
lab's edit events once a day, from a per-lab watermark in `lexicon_watch_state`.

- `added_text()` and `candidates()` — phrases, acronyms and long words an edit added, never filler
- `scan_edits()` keeps those the lexicon and its synonyms do not cover, counted in `potential_lexicon_term`
- `approve()` makes the next lexicon version (`knowledge/lexicon_versions.py`); `reject()` sets terms aside.
  A radiologist decides, from `/lexicon` or the `/ui/lexicon` screen

### `onboarding/paired_audio.py` — S4, verbatim annotation
The project's critical path. The ASR bake-off needs verbatim ground truth on the
`current` capture class, every release gate needs the bake-off, and the pipeline
needs the gates.

- `build_verbatim_queue()`, `submit_verbatim()`, `link_corpus_report()`
- `gold_partition_progress()` and `corpus_hours()` — tracks `legacy` and `current` apart
- `mine_surface_variants()` — feeds the variants back into S3

### `onboarding/boilerplate.py` — S5, boilerplate mining and ranking
Ranks the normal-finding phrases a lab reuses, for the radiologist's Pass 2
session. The UI is deferred to a CSV export; the table is not deferred, because
the ranking has to accumulate from the first corpus load.

- `mine_boilerplate()` and `rank_candidates()`
- `export_candidates_csv()` for the pilot
- `promote_candidate()` / `reject_candidate()` — the `absence_policy` decision, kept explicit

### `onboarding/critical_rules.py` — S6, critical-findings rules
Regulatory (R9), and not cuttable. The alert path has to exist before the review
queue does, because the exposure starts the moment a draft can sit unread.

- `seed_candidate_rules()` from the corpus, `author_rule()`, `approve_rule()`
- `matches()` / `active_rules()` — the matcher the pipeline's stage 7 consumes
- S7 blocks the pilot on at least one approved active rule

### `onboarding/readiness.py` — S7, the pilot gate
Turns a checklist into a structural precondition: `tenant.status` cannot reach
`pilot` while any `fail`-severity check is outstanding.

- Seven checks: collision audit clear, corpus→template coverage, voice enrollment,
  gold set frozen, critical rules approved, baseline CSE measured, template library ready
- `load_facts()` reads everything the checks look at in one statement of scalar subqueries; each check then judges those `ReadinessFacts` without touching the database
- `evaluate_readiness()` returns the report the admin panel's readiness page and its JSON API both render

---

## 3. Capture

The capture-only path. Ships before anything that interprets audio, so the
`current` gold partition accumulates while the rest is being built — and must
keep working when every other module is down.

**6 files, 547 lines.** (2 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 186 | `ingest/audio_gates.py` | §9.8 quality gates: format, SNR, silence, duration |
| 130 | `ingest/service.py` | Capture-only ingest, idempotent by content hash |
| 59 | `ingest/studies.py` | Registers the study a dictation belongs to, and its patient, by accession number, as a hospital system would send them |
| 170 | `adapters/storage/object_store.py` | S3-compatible store (SSE-KMS) + in-memory double |

### `ingest/audio_gates.py` — §9.8 quality gates
Two kinds of failure, kept apart deliberately. A reject is permanent data loss;
a warning is a recording worth keeping with a caveat attached.

- **Reject**: lossy codec, unsupported container — lossy audio can never train ASR later
- **Warn**: low sample rate, poor SNR, mostly silence, odd duration
- `sniff_container()`, `probe_audio()`, `analyse_frames()`, `estimate_snr_db()`, `estimate_silence_ratio()`

### `ingest/service.py` — capture-only ingest
Record → validate → FLAC → object store → `recording` row → audit. No pipeline,
no ASR, no UI, on purpose.

- `ingest_recording()` — idempotent by `content_hash`, so a retried upload is a no-op;
  the lab's Bloom filter (`cache/filters.py`) skips the duplicate lookup when the hash is definitely new
- The service itself starts nothing; the upload route queues the `run_pipeline` job (see [Background work](#9-background-work))
- Classifies `capture_device_class` (`legacy` / `current`), which decides gold-set partitioning
- `force_legacy_device_class()` for backfilling historical uploads

### `adapters/storage/object_store.py` — S3-compatible object store
Audio is retained indefinitely as lossless FLAC. The clinical archive and the
pseudonymised training corpus are separate stores because they rest on different
legal bases.

- `ObjectStore` interface with `S3ObjectStore` and `InMemoryObjectStore`
- `audio_key()` — tenant-prefixed keys, belt-and-braces against the RLS layer
- Streaming put/get so a long dictation never lands wholly in memory

---

## 4. Pipeline

The clinical core: a recording in, a persisted draft out. §8.3's fifteen stages
plus a persist step, and the stages marked *(deterministic)* below are
deliberately the ones carrying the safety properties — replayable,
bit-reproducible, testable with no model bound. The rest call a model or an ASR
engine, and each degrades explicitly when none is bound.

**41 files, 5,582 lines.** (3 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 260 | `pipeline/stages/routing.py` | Stage 9 — the §8.3.4 cascade. Declines rather than picking the nearest of twenty. |
| 245 | `pipeline/stages/normalise.py` | Stage 3 — phonetic resolution with the margin guard (stops LMC resolving to LMP) |
| 216 | `pipeline/stages/extract.py` | Stage 10 — per-section extraction, k=3, provenance mandatory |
| 172 | `pipeline/stages/verify/rules.py` | Stage 13 — the deterministic §8.3.8 contradiction checks |
| 186 | `pipeline/stages/persist.py` | Stage 16 — the seam to the review surface; emits every domain row |
| 115 | `pipeline/stages/release.py` | Stage 17 — files the report a granted class released (GA, off by default) |
| 187 | `pipeline/stages/critic.py` | Stage 13b — LLM critic + round-trip entailment (Beta) |
| 188 | `pipeline/stages/reconcile.py` | Stage 2b — multi-engine fan-out, ROVER, bounded arbitration (Beta) |
| 184 | `pipeline/stages/repairs.py` | Stage 6 — self-corrections. Splits the utterance; never deletes. |
| 215 | `knowledge/phonetics.py` | Double metaphone, the E-set, the collision audit |
| 175 | `pipeline/stages/study_code.py` | Stage 4 — bounded study-code search; `CODEWORD_COMPLIANCE` vs `STUDYCODE_RECALL` |
| 145 | `pipeline/stages/grounding.py` | Stage 11 — verbatim quote check, outside the LLM (invariant I1) |
| 164 | `pipeline/stages/segment.py` | Stage 5 — segment and classify utterances. Labels only; nothing deleted. |
| 167 | `pipeline/stages/critical.py` | Stage 7 — critical findings on the transcript; writes the alert row |
| 178 | `pipeline/stages/preprocess.py` | Stage 1 — VAD, SNR, peak normalisation |
| 169 | `pipeline/graph.py` | The orchestrator: `stage_execution` rows, commit discipline, shadow mode |
| 252 | `pipeline/state.py` | `PipelineState` — the object every stage reads |
| 124 | `pipeline/stages/providers.py` | Per-tenant knowledge snapshot, injected (stages get no DB session) |
| 152 | `pipeline/stages/post_correction.py` | Stage 2c — the only stage allowed to rewrite the transcript |
| 153 | `knowledge/consent.py` | Derived training eligibility; `G6_legal_basis` |
| 221 | `knowledge/languages.py` | Radiology terms said or typed in Hindi (Devanagari or Latin script), French or Spanish, mapped to the English term |
| 155 | `knowledge/synonyms.py` | Two different words for the same finding ("consolidation", "infiltrate"); optional RadLex lookups |
| 122 | `knowledge/variant_review.py` | A heard phrase's match to a term: use it, ask a radiologist, or hide it |
| 119 | `pipeline/runner.py` | Runs the pipeline for one recording from what the database holds, as the `run_pipeline` job |
| 115 | `knowledge/training_data.py` | Radiologists' approvals as training examples for a lab-tuned template model, from labs that agreed |
| 98 | `knowledge/term_lookup.py` | Which of a lab's lexicon terms a piece of text refers to: exact, short form, heard variant, synonym, or translated |
| 64 | `knowledge/bloom.py` | A Bloom filter: "definitely not present" or "maybe present", no dependency |
| 45 | `knowledge/lexicon_versions.py` | Which lexicon version is current, and making the next one |
| 125 | `pipeline/stages/asr.py` | Stage 2 — single-engine ASR with keyterm biasing |
| 130 | `pipeline/v1.py` | The 16-stage graph, plus release at GA. The only place edges are defined. |
| 139 | `pipeline/stages/compose.py` | Stage 12 — deterministic render from `render_spec`, grounded atoms only |
| 135 | `pipeline/stages/sketch.py` | Stage 8 — template-free finding sketch, before routing |
| 125 | `pipeline/stages/route_human.py` | Stage 15 — who reviews it, the §5.4.1 grading sample, and whether anybody reviews it |
| 99 | `pipeline/stages/confidence.py` | Stage 14 — `min(critical) × mean(all)` |
| 83 | `pipeline/timing.py` | Character offset → audio time, for click-to-listen and the training corpus |
| 77 | `pipeline/context.py` | `RunContext`: cost accounting, budget cap, model resolution |
| 75 | `pipeline/contracts.py` | `Stage` / `StageResult` — no stage writes domain tables |
| 5 | `pipeline/stages/verify/__init__.py` | Re-exports the verification stage |

`knowledge/data/` holds the curated lists those modules load: `synonyms.csv`
(44 lines) and `languages/hi.csv`, `fr.csv`, `es.csv` (52, 26 and 27 lines).

### Orchestration

#### `pipeline/contracts.py` — the stage contract
Every stage persists a `stage_execution` row through the context and writes no
domain tables; the orchestrator commits. That is what keeps replay and shadow
mode honest.

- `Stage` protocol, `StageResult`, `StageContext`
- A shadow run cannot pollute production data, because stages have nothing to write with

#### `pipeline/state.py` — `PipelineState`
The single object threaded through the graph. Every stage reads from it and
returns a new slice of it.

- Audio meta, ASR refs, utterances, word timings, term resolutions
- Transcript state, finding sketch, routing state, field values with provenance
- Verification findings and critical alerts

#### `pipeline/context.py` — the run context
What a stage is handed instead of a database session, plus the two cross-cutting
concerns Phase 0 required.

- Cost accounting per stage, rolled onto `pipeline_run.total_cost_usd`
- Per-stage timing, attributed per template and per radiologist
- `resolve_model()` so no stage names a model

#### `pipeline/graph.py` — the orchestrator
The machinery stages plug into, built so adding one is registration rather than
restructuring.

- `PipelineGraph` runs `StageSpec`s in declared order against `PipelineState`
- One `stage_execution` row per stage, with cost, tokens and the resolved model id
- `new_run()` opens the `pipeline_run`; failure handling and partial-run recording

#### `pipeline/runner.py` — the `run_pipeline` job
How a recording actually reaches the graph. An upload queues the job in the same
transaction as the `recording` row; `python -m radreport.workers` claims it and
calls this module with a session already bound to the job's lab.

- `load_template_candidates()` reads the lab's routable templates
- `default_graph_factory()` builds the graph with the configured engines; `set_graph_factory()` replaces it in tests
- `run_recording()` creates the run, executes it, and emits `draft.ready` (and `report.signed` for a released report)
- A failed stage is recorded on its `pipeline_run` and the job still completes: retrying the same input reproduces the same failure

#### `pipeline/v1.py` — the edges
The only place the stage order is defined. Adding or reordering a stage is a
change to this file and nothing else.

- Builds the V1 graph, and the Beta graph when reconcile/post-correction/critic are configured
- Swaps `AsrStage` for `ReconcileStage` when more than one engine is bound
- Degrades explicitly — no LLM client means the LLM stages announce it rather than silently skipping

#### `pipeline/timing.py` — character offset → audio time
The map everything human-facing depends on. Without it, click-to-listen plays
from 0 ms for every field and nobody notices.

- `build_timing_map()`, `audio_span()`, `coverage()`
- Feeds per-field playback (§7.2) and `edit_event.audio_start_ms`

#### `pipeline/stages/providers.py` — per-tenant knowledge, injected
Stages get no session, but several need tenant-scoped reads. Those are resolved
up front and handed in, the same shape as model resolution.

- `TenantKnowledge`: lexicon entries, spoken study codes, approved critical rules
- `KnowledgeProvider` / `StaticKnowledgeProvider`, `load_tenant_knowledge()`

### Stages

#### `stages/preprocess.py` — 1, VAD and normalisation *(deterministic)*
Produces the speech regions the ASR call is billed on, which is a different
question from ingest's accept/reject silence check.

- `detect_speech_regions()`, trimming pre-roll
- Emits `SpeechRegion`s and the normalised audio handed to the engine

#### `stages/asr.py` — 2, transcription with keyterm biasing
Takes whichever engine it is handed and records exactly which one ran. The
engine is chosen by the bake-off, not here.

- `build_keyterms()` from the active lexicon, biasing recognition toward lab vocabulary
- Writes `asr_run` with the engine id and `config_hash`, so a vendor update can be re-gated

#### `stages/reconcile.py` — 2b, ROVER fan-out *(Beta)*
Replaces `AsrStage` when more than one engine is configured. The single-engine
stage stays; a lab that cannot afford three transcriptions should not be forced
into them.

- Concurrent fan-out across `EngineSpec`s, failure-tolerant — one timeout degrades, never fails
- Hands the hypotheses to `adapters/asr/rover.py` for voting

#### `stages/post_correction.py` — 2c, phonetic post-correction *(Beta)*
The one stage permitted to rewrite the transcript, and only because of where it
sits — before segmentation assigns labels to spans.

- `correct_transcript()` applies S4's mined surface variants ("echo texture" → "echotexture")
- Records every `Correction` so the rewrite is auditable

#### `stages/normalise.py` — 3, phonetic resolution and margin guard *(deterministic)*
The cheapest safety control in the pipeline. `LMC` and `LMP` differ by one
confusable E-set letter and both read fluently in a signed report.

- `resolve_spans()` against the lab lexicon by phonetic key
- Refuses any match whose margin over the runner-up is too small — `escalations()` instead of a guess
- Never rewrites the transcript; it annotates

#### `stages/study_code.py` — 4, study-code detection *(deterministic)*
The primary routing anchor, searched in a bounded window rather than the whole
dictation — an unbounded scan finds "chest CT" in the body text.

- `detect_study_code()` behind the carrier phrase, first ~20 seconds
- `compliance_metrics()` — feeds `CODEWORD_COMPLIANCE`, the number that decides D1

#### `stages/segment.py` — 5, utterance segmentation and labelling
Says what each span is; never removes one. Every span gets one of §6.4's labels.

- `parse_response()` / `fallback_segments()` when no model is bound
- `apply_audio_bounds()` keeps labels aligned to word timings
- `apply_inclusion()` decides what reaches extraction, without deleting the rest

#### `stages/repairs.py` — 6, self-corrections *(deterministic)*
*"left — sorry, right kidney."* Naive disfluency filtering keeps `left`, which is
a G4 error class in a signed report.

- `detect_repairs()` and `apply_repairs()` — nothing deleted, the retracted span marked superseded
- `retracted_spans()` surfaces them in the review UI so a human can see what was dropped

#### `stages/critical.py` — 7, critical findings and alerting *(deterministic)*
Runs before routing and extraction, because the alert must not depend on
choosing the right template.

- `detect_alerts()` against the lab's approved rules, `highest_severity()`
- `bypasses_queue()` — a critical alert does not wait for a reviewer to reach it
- `scanned_labels()` records what was in scope, so a miss is diagnosable

#### `stages/sketch.py` — 8, template-free finding sketch
Surfaces the clinical assertions without a template in hand, so routing's
coverage validation has something to check against.

- `build_prompt()` / `parse_response()`, `fallback_sketch()` with no model bound
- An assertion the chosen template cannot hold is §8.3.4's wrong-template signal

#### `stages/routing.py` — 9, the routing cascade
Six steps ordered so the cascade stops early: each is cheaper and more certain
than the next. A heard study code routes outright and calls no model.

- `hard_filter()` → `demographic_filter()` → `hybrid_rank()` → LLM picks from top-5
- `attach_modules()` then coverage validation against the sketch
- `ShortlistPicker` is the only model call, and only when the cheap steps fail

#### `stages/extract.py` — 10, per-section extraction, k=3
The most expensive stage and the one with the most safety machinery. Per
section, not per report — a section's fields co-occur in a sentence.

- `build_prompt()` ordered stable → cache breakpoint → volatile
- k=3 self-consistency samples; `merge_samples()` votes, `parse_fields()` requires a cited span
- Separate sections share a cached prefix; see `adapters/llm/sampling.py` for the ordering trap

#### `stages/grounding.py` — 11, grounding and coverage *(deterministic)*
Deliberately outside the LLM. A model asked to verify its own grounding will
confirm it, because the process that invented the value invents the
justification.

- `quote_is_verbatim()` against the transcript; `normalise_for_comparison()`
- `ground_field_values()` enforces invariant I1's three checks
- `renderable()` — the only thing compose is allowed to see

#### `stages/compose.py` — 12, compose the report text
Input restricted to grounded atoms, enforced by what compose is *given*: it
reads `grounding.renderable(state)`, so an ungrounded value is never in front of
it to decline.

- `render_value()` and `RenderSpec` — deterministic rendering per field type
- Produces the `ComposedReport` the reviewer reads

#### `stages/verify/rules.py` — 13, deterministic verification *(deterministic)*
V1 ships these and nothing else. A deterministic check that fires is evidence;
an LLM critic that fires is an opinion, and a screen full of opinions teaches
reviewers to dismiss the panel.

- Laterality agreement, negation vs. assertion, measurement plausibility and source match
- Nothing auto-filled; ungrounded values not renderable; enum values in schema
- `run_checks()` → `summarise()` into the flagged-field count the queue sorts on

#### `stages/critic.py` — 13b, LLM critic and entailment *(Beta)*
Added once there is a measured miss rate for the deterministic rules to justify
the extra opinion.

- `parse_critic()` and `parse_entailment()` — round-trip entailment of the composed text
- Findings land alongside, never replacing, the deterministic ones

#### `stages/confidence.py` — 14, report confidence *(deterministic)*
One line of arithmetic: `min(critical fields) × mean(all fields)`. Averaging
hides the one case that matters.

- `compute_confidence()` + `ConfidenceBreakdown` for the UI
- `needs_radiologist()` — the threshold stage 15 reads

#### `stages/route_human.py` — 15, route to a human *(deterministic)*
Decides who sees the draft, from three inputs in priority order — and, at GA,
whether anybody does.

- A critical finding routes to a radiologist regardless of confidence
- Then confidence, then the §5.4.1 grading sample — `is_sampled_for_grading()`, `grading_rate()`
- `decide()` writes the `routing_decision` the queue reads
- **Phase 6:** a fourth outcome *after* those three — a `granted` autonomy class
  can remove review entirely. Consulted only on the assistant path, so the
  aggregate argument can never override a per-report rule

#### `stages/persist.py` — 16, persist the draft
The seam between the pipeline and the review surface — the one that was missing
while both halves were tested separately.

- `PersistDraftStage` turns `PipelineState` into `report_draft` + field values + provenance
- Writes the flagged counts and alerts the review queue orders by
- A released draft is born `signed`, so it never reaches the queue

#### `stages/release.py` — 17, release without review *(GA, off by default)*
Files the `final_report` for a draft stage 15 decided to release. Behind
`enable_autonomous_release`: letting reports go out unreviewed must be something
a deployment turned on, never something it inherited by upgrading.

- `AutonomousReleaseStage` emits the report through `pending_writes`, so a
  shadow run discards it like any other domain write
- Never re-decides — reads `state.human_routing` — and raises rather than
  filing a row with a gap in it

### Knowledge

#### `knowledge/phonetics.py` — phonetic keys and collision audit
Spelled-out letters cluster into the confusable E-set (B, C, D, E, G, P, T, V,
Z), which all rhyme. Double metaphone alone maps `LMC` and `LMP` to different
keys, so a naive same-key check finds nothing.

- `double_metaphone()`, `is_spelled_acronym()`, `confusable_letters()`
- `acronym_distance()` / `phonetic_distance()` / `normalised_levenshtein()`
- `audit_collisions()` and `resolve_with_margin_guard()` — shared by S3 and stage 3

#### `knowledge/consent.py` — training eligibility, derived
`recording.is_training_corpus_eligible` is computed, never set. Four independent
conditions, re-derivable at any time.

- `evaluate_eligibility()`, `derive_training_eligibility()`, `rederive_for_tenant()`
- `record_consent_event()` — the audit chain behind a withdrawal
- `verify_g6_legal_basis()` — the adaptation gate reads this, not a boolean column

#### `knowledge/term_lookup.py` — text to lexicon term
Used by the term watcher to decide whether a phrase is already known.

- `lab_terms()` builds the lab's lookup once per request
- `find_term()` tries exact forms, then the curated synonym set, then the English for a term in one of the lab's languages
- `is_unknown()` — the lab's Bloom filter answers first when it can say "definitely not"

#### `knowledge/synonyms.py` and `knowledge/languages.py` — same finding, other words
Sound-alike matching cannot tell that "consolidation" and "infiltrate" mean the
same finding, or that a Hindi word is a known term.

- `synonyms.py` — `concept_of()`, `semantic_similarity()` over `data/synonyms.csv`;
  `RadLexClient` only with `BIOPORTAL_API_KEY`. The local set carries no external
  codes: an invented SNOMED or RadLex id is worse than none
- `languages.py` — a lab turns languages on in its settings; `glossary()` loads
  `data/languages/{hi,fr,es}.csv`, `find_foreign()` takes the longest known phrase,
  `translate()` returns spans with their English. An online translator, when
  configured, gets one unknown Devanagari word at a time, never the sentence

#### `knowledge/variant_review.py` — confident matches only
- `match_confidence()` — the best of synonym similarity and a blend of sound-alike and spelling distance
- `decide()` — `auto_approved` above the auto threshold, `pending` above the review threshold, hidden below
- `threshold_arm()` places a lab in an experiment arm; `record_review()` counts an override when a radiologist reverses an automatic approval; `arm_stats()` compares arms

#### `knowledge/lexicon_versions.py`, `bloom.py`, `training_data.py`
- `current_set()` — the filter every reader uses: a set with no newer version of the same name;
  `new_version()` copies terms and variants forward, in batched inserts
- `BloomFilter.for_capacity()` — sized from item count and false-positive rate; double hashing over one blake2b digest
- `training_data.export()` — approved templates with their source text, sound-alike answers and
  new-term decisions as JSONL, split by a stable hash, only from labs with `training.share_approvals`
  on. Nothing from a report or transcript leaves

---

## 5. Engines

The only place a vendor is named. Everything above calls an interface; swapping
a provider is configuration, not an engineering project.

**16 files, 1,542 lines.** (3 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 314 | `adapters/asr/rover.py` | ROVER multi-engine voting. NULL is a candidate, which suppresses single-engine insertions. |
| 172 | `adapters/llm/response_cache.py` | Answers a repeat of an identical model request from a stored reply, so re-running a recording does not pay twice |
| 161 | `adapters/llm/registry.py` | Resolves "which model serves task T for tenant X"; refuses activation without a gold-set eval run |
| 136 | `adapters/llm/anthropic_client.py` | Anthropic client with cache-control blocks and usage accounting |
| 113 | `adapters/llm/prompt.py` | `PromptBundle` — makes a cache-hostile prompt order unexpressible |
| 97 | `adapters/llm/openai_compat.py` | OpenAI-compatible client, for locally hosted models |
| 109 | `adapters/asr/whisper_local.py` | faster-whisper behind the engine interface, plus the deterministic stub |
| 113 | `adapters/llm/base.py` | `LLMClient` protocol, request/response/usage types |
| 72 | `adapters/llm/sampling.py` | k-sample fan-out with cache warm-up (sample 1 completes before 2..k) |
| 105 | `adapters/llm/concurrency.py` | Concurrency limiter and circuit breaker |
| 57 | `adapters/llm/pricing.py` | Corrected Sonnet 5 pricing and cached-call cost accounting |
| 61 | `adapters/asr/base.py` | `ASREngine` protocol, word timings, config hashing for idempotent reruns |
| 29 | `adapters/llm/factory.py` | Builds the client for a resolved model: Anthropic SDK for Anthropic, the OpenAI-compatible client for everything else |

### `adapters/llm/base.py` — the LLM interface
One interface over cloud and local providers. A local model is a row in
`model_definition` with an `endpoint_override`, not a second code path.

- `LLMClient` protocol, `LLMRequest` / `LLMResponse`, `BatchRequestItem`
- `Usage` and `ResolvedModelRef` — cost and provenance recorded on every call

### `adapters/llm/prompt.py` — prompt construction with cache ordering
The rule `[stable: system, schema, exemplars] → [breakpoint] → [volatile:
transcript]` is a type here, not a convention. Retrofitting it means
restructuring every prompt and re-running the gate.

- `PromptBundle` makes the wrong order unexpressible
- `system_block()`, `schema_block()`, `section_block()`, `exemplar_block()`
- Rejects unpinned exemplars in the stable region — they invalidate the prefix while looking cached

### `adapters/llm/sampling.py` — k-sample self-consistency
Sample 1 must complete before 2..k fire. The obvious `asyncio.gather` over all k
forfeits ~29% of the LLM bill, silently.

- `sample_k()` with the cache warm-up ordering, `majority_vote()` over `SampleSet`
- Varies temperature and seed only — samples 2..k are exact prefix repeats

### `adapters/llm/registry.py` — task → model resolution
Every call site asks which model serves task `extraction` for tenant T; none
names a model. Skipping this hardcodes a model id in seven places.

- `TaskModelResolver` → `ResolvedModel`, per tenant per task
- `activate_assignment()` refuses activation without a gold-set `eval_run` — a rule Postgres `CHECK` cannot express
- Resolutions are read through the shared cache (`cache/shared.py`), five minutes shared and 30 seconds local

### `adapters/llm/response_cache.py` — repeat requests answered from storage
- `request_fingerprint()` covers everything that changes the answer, including the model id
- `cacheable()` — a sampled request with no seed is meant to vary, so it is never cached; k-sample sets stay distinct
- `CachingLLMClient` over `PostgresResponseStore` (`llm_response_cache`, lab-isolated) or `SharedResponseStore`;
  `with_response_cache()` wraps a client as configured. A hit costs nothing and reports what it saved

### `adapters/llm/factory.py` — client construction
- `client_for()` — `AnthropicClient` for an Anthropic model, `OpenAICompatibleClient` for everything else

### `adapters/llm/pricing.py` — cost accounting
Makes the real per-stage number observable, which is the only way the
optimisation ladder gets verified rather than asserted.

- `cost_usd()` with cache tiers and the Batch API discount
- `derive_cache_prices()`, `savings_vs_uncached()`, `SeedPrice` for the catalog

### `adapters/llm/concurrency.py` — rate limiting and circuit breaking
Covers saturation, which the design doc's outage handling does not. An unbounded
retry storm against a rate limit looks exactly like an outage.

- `ProviderLimiter` — concurrency ceiling and backpressure per provider
- `CircuitBreaker` — opens on sustained failure rather than retrying into it

### `adapters/llm/anthropic_client.py` — Anthropic client
Written against the current API shape, which the design doc predates.

- `thinking: {type: "adaptive"}` plus `output_config.effort`; `budget_tokens` is rejected
- Structured output via `output_config.format`; tool schemas take top-level `strict: true`
- No assistant prefill

### `adapters/llm/openai_compat.py` — OpenAI-compatible client
What makes "local models are just rows with an `endpoint_override`" true rather
than aspirational.

- `OpenAICompatibleClient` against any OpenAI-shaped endpoint
- Lets the bounded tasks move to open-weight models as a configuration change

### `adapters/asr/base.py` — the ASR interface
Phase-0 infrastructure, not a pipeline stage: S4's bootstrap run needs it, S4
gates the gold set, and the gold set gates everything.

- `ASREngine` protocol, `ASRConfig`, `ASRResult`, `Word` with timings
- Shaped for a bake-off, because Phase 2 runs one across several engines

### `adapters/asr/whisper_local.py` — local Whisper
The one engine behind the interface today. Medium is the default over large-v3
on the design doc's own numbers: 13.2% WER against 19.0% on Indian-accented
speech, with insertions at 50.7% of large-v3's errors.

- `WhisperLocalEngine` and `StubASREngine` for tests and no-model runs
- An invented word in a clinical transcript is not a smaller error than a missed one

### `adapters/asr/rover.py` — multi-engine voting
Recognizer Output Voting Error Reduction. Two engines rarely make the same
mistake, and where they agree the word is almost certainly right.

- `choose_base()`, `build_network()`, `reconcile()` — word transition network and per-slot vote
- `DisputedSpan` + `arbitration_payload()` / `apply_arbitration()` for the slots voting cannot settle
- `from_asr_results()` adapts the engine outputs into hypotheses

---

## 6. Review and export

The human loop and what leaves the building. The schema for this was designed in
Phase 0; this module is the behaviour.

**10 files, 1,301 lines.** (2 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 343 | `review/session.py` | Open a draft, record revisions, `active_edit_seconds`, categorised edit events |
| 191 | `review/signing.py` | The four refusals between a draft and a signed record; addenda |
| 160 | `export/hl7.py` | HL7 v2 ORU^R01, MLLP-framed |
| 148 | `review/grading.py` | G0–G4, CSE rate, and the feed into autonomy accrual + CUSUM |
| 131 | `export/fhir.py` | FHIR R4 DiagnosticReport + transaction bundle |
| 142 | `review/queue.py` | Priority → alert → flagged count → oldest, filtered by role |
| 80 | `review/feedback.py` | §9.6's "this draft was useless", actually recorded |
| 104 | `review/rbac.py` | The four roles, genuinely different (an assistant may not sign) |
| 1 | `review/__init__.py` | Package docstring |

### `review/rbac.py` — the four roles
Stated as code because the instinct is to collapse them into "can edit" and
"can't", and three of the four distinctions are load-bearing.

- `Reviewer`, `Permission`, `require()`, `PermissionDenied`
- A radiologist assistant may revise but may not sign — two-layer supervision is the safety model
- Enforced server-side on every route, never from a header

### `review/queue.py` — the review queue
Flagged-first ordering, which `report_draft.flagged_field_count` exists to
drive: a reviewer who reads thirty near-perfect drafts stops reading carefully.

- `build_queue()` and `sort_key()` — priority beats flags, flags beat age
- `queue_stats()`, `count_by_status()` for the dashboard

### `review/session.py` — opening and revising a draft
Produces the most commercially valuable data in the system, and two fields carry
that value.

- `open_draft()` → `DraftView` with fields, provenance and `Retraction`s
- `record_revision()` + `categorise_edit()` — per-field edit events with audio offsets
- `active_edit_seconds` is focus time, not wall clock; the break-even bar is 18–36 seconds

### `review/signing.py` — signing and amendment
The moment a draft becomes a legal medical record, so the gates are refusals
rather than warnings.

- `preflight()` → `SigningChecks.may_sign`, so the button and the call agree; `SigningRefused` otherwise
- Four gates: only a radiologist signs, no blocking verification finding, no unacknowledged
  critical alert (`acknowledge_alert()`), no ungrounded value
- `final_report` is immutable and content-hashed; `amend_report()` writes an addendum pointing at
  the report it amends

### `review/grading.py` — G0–G4
A judgement about clinical significance, not edit volume. G3 and G4 are the CSE
set, and the CSE rate is what every non-inferiority calculation is made of.

- `grade_report()` on the §5.4.1 sampling schedule
- `cse_rate()` → the number autonomy accrual and the release gate both read

### `review/feedback.py` — "this draft was useless"
The affordance plus the half that usually goes missing: somewhere for the answer
to go, and a number someone looks at.

- `report_usefulness()`, `usefulness_stats()`
- Flywheel-stall detection: a reviewer who rewrites from scratch stops engaging

### `export/hl7.py` — HL7 v2 ORU^R01
The format every RIS in an Indian radiology practice already speaks. Built by
hand because the message emitted here is one narrow shape.

- `build_oru()` from a signed `final_report` + `OruContext`
- `escape()` and `hl7_timestamp()` for the encoding rules
- `ExportRefused` — an unsigned report cannot be exported

### `export/fhir.py` — FHIR R4 DiagnosticReport
The modern half. Both exist because a deployment does not get to choose which
system its customer runs.

- `build_diagnostic_report()` and `build_bundle()` as plain dicts
- `FhirContext` carries the patient, practitioner and accession references

---

## 7. Governance

Measurement, and the machinery that decides what the rest of the system is
allowed to do unsupervised. Reads from everything; called by nothing.

**19 files, 2,263 lines.** (4 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 264 | `autonomy/accrual.py` | Evidence gathering and the Beta-Binomial posterior |
| 219 | `eval/goldset.py` | §5.3-stratified assembly, freeze, permanent training exclusion |
| 262 | `eval/bakeoff.py` | ASR bake-off: per-partition, insertions tracked independently |
| 189 | `monitoring/drift.py` | PSI against an explicit baseline window |
| 204 | `monitoring/costs.py` | What the pipeline costs, per lab and per stage, and the days that cost far more than usual |
| 174 | `autonomy/grant.py` | Bayesian sequential grant, mechanical CUSUM revocation |
| 67 | `autonomy/classes.py` | Defines the report classes autonomy is earned for, each with the measured baseline error rate it must not exceed |
| 174 | `adaptation/gates.py` | §8.6.5's six gates; two unimplemented and failing closed |
| 176 | `eval/harness.py` | Eval runner, scopeable to one `task_key` |
| 119 | `eval/gates.py` | Release-gate evaluation with per-stratum breakdowns |
| 95 | `eval/metrics/asr_metrics.py` | WER, INS_RATE, CTER — all from one alignment |
| 75 | `eval/metrics/routing_metrics.py` | Routing accuracy, codeword compliance, study-code recall |
| 69 | `eval/metrics/alignment.py` | Token alignment shared by the ASR metrics |
| 51 | `eval/metrics/__init__.py` | Metric registry |
| 121 | `autonomy/release.py` | The release gate: what a grant actually changes, and §4.3's coverage |
| 1 | `autonomy/__init__.py` | Package docstring — Beta observes, Phase 6 grants |

### `eval/harness.py` — the eval harness
Precedes the pipeline by invariant I6: you cannot tell whether a stage works
without something to measure it with, and retrofitting means every earlier stage
was built blind.

- `EvalHarness` over `EvalContext` and `StageOutputs`
- Scopeable to one `task_key`, so a per-task model swap can be measured alone
- `assert_no_eval_leakage()` — training data must not reach the eval set

### `eval/goldset.py` — gold-set assembly
Stratified by `capture_device_class`, because a WER measured on the lab's old
handhelds predicts nothing about the microphones about to arrive.

- `eligible_candidates()`, `assemble()`, `freeze()` — an immutable set every gate reads
- `quality_bucket()`, `partition_summary()`
- `assert_no_training_leakage()` enforces the R21 exclusion

### `eval/bakeoff.py` — the ASR bake-off
Refuses to produce a single score, on the strength of §7.6: a combined number
would have picked large-v3, the model that invents words.

- `run_bakeoff()` per partition, per engine → `EngineResult` / `PartitionResult`
- Insertions reported independently of WER, never folded in
- `has_non_latin_script()` and `format_report()` for the decision record

### `eval/gates.py` — release gates
No pipeline version reaches production unless a release-gate `eval_run` shows no
regression on CSE_DRAFT, HALLUC_RATE or ROUTE_TOP1 per template.

- `evaluate_gate()` → `GateVerdict`, per `GateMetric`
- `Direction` per metric — ROUTE_TOP1 regresses downward, HALLUC_RATE upward

### `eval/metrics/` — one module per §5.2 metric
The registry plus the metrics computable from stored artefacts alone. Two
separations here are structural, not stylistic.

- `__init__.py` — `Metric` protocol, `MetricRegistry`, `default_registry`; task-scoping so a
  routing-only run does not report an empty WER as a result
- `alignment.py` — `tokenize()` / `align()`, shared so WER and INS_RATE come from one alignment
- `asr_metrics.py` — `WordErrorRate`, `InsertionRate`, `ClinicalTermErrorRate`
- `routing_metrics.py` — `CodewordCompliance` (did they say it) vs. `StudyCodeRecall` (did we
  hear it) vs. `RouteTop1`

### `autonomy/accrual.py` — observation only
Records evidence and computes how much of it exists. Grants nothing. Evidence
gathered before anyone can act on it is evidence nobody was tempted to shape.

- `record_observation()`, `snapshot()` → `AccrualSnapshot`
- `posterior_non_inferiority()` against the measured baseline
- `open_accrual()` starts a window

### `autonomy/grant.py` — grant and revocation
The asymmetry is the design: granting is deliberate and hard, revocation is fast
and available to the party at risk.

- `grant()` needs volume, posterior above threshold, a measured baseline and a named human
- `revoke()` / `suspend()` — a lab admin may revoke its own autonomy; only a product admin grants it
- `cusum_increment()` / `step_cusum()` / `observe_graded_report()` — continuous CUSUM monitoring

### `autonomy/release.py` — the gate that acts on a grant
The seam that was missing until Phase 6: `autonomy_class.status` was written by
`grant.py` and read by nothing, so a granted class behaved exactly like an
accruing one and §4.3's review reduction had no implementation.

- `may_release_without_review()` → `ReleaseDecision` with a stable `blocker` code
- **The §5.4.1 grading sample is never released.** Grading feeds the CUSUM and
  the CUSUM is the only thing that can revoke, so releasing the sample would
  freeze the monitor at the moment the grant lands — and it would keep
  reporting as coverage
- `AUTONOMOUS_RELEASE_THRESHOLD = 0.90`, above §8.3.7's 0.70. That number is the
  bar for "an assistant may review this", which still has a human reading every
  word; it was never calibrated for "nobody does". **Not from the design doc** —
  a policy constant stated here with its rationale
- Also gated on `radiologist_profile.autonomy_enabled`, which nothing read
  before: a class-level grant is not consent from the person whose name is on
  the report (§10.7)
- `coverage()` → `Coverage`, measuring §4.3's target over *signed* volume

### `adaptation/gates.py` — §8.6.5's six prerequisite gates
Nothing trains until all six pass, and the result is recorded against the
adaptation run so a promoted model traces back to the evidence that permitted it.

- Implemented: `gate_g1_volume`, `gate_g3_speaker_balance`, `gate_g4_hardware_homogeneous`,
  `gate_g6_legal_basis`
- **G2 and G5 are unimplemented and fail closed** — their text is in neither `PLAN.md` nor the
  design PDF
- `evaluate_gates()` → `GateReport`; `require_gates()` raises `AdaptationBlocked`

### `monitoring/drift.py` — drift monitoring
A vendor-side model change is a pipeline change that must re-clear the release
gate. This notices when something changed and nobody said so.

- `population_stability_index()` against an explicit baseline window, not a mean comparison
- `categorical_drift()` for template and routing distributions
- `evaluate_drift()` → `DriftReport`: silent model updates, a new microphone, a locum's accent

### `monitoring/costs.py` — the cost dashboard
Makes per-lab and per-stage spend visible across labs, which a lab-bound session
cannot read on its own.

- `daily_costs()` / `stage_costs()` call `tenant_daily_cost()` and `tenant_stage_cost()`,
  functions owned by the view-owner role (migration 0017)
- `detect_spikes()` against a trailing window; `platform_summary()` and `lab_summary()` for `/admin/costs`
- `scan_for_anomalies()` — the `cost_anomaly_scan` job emits each new spike as a `cost.anomaly` event

---

## 8. Surfaces

HTTP and the admin panel. Thin by rule — access checks, permission checks and
serialisation, no business logic.

**36 files, 6,936 lines**, plus the access policy XML and four static assets.
(5 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 778 | `api/routes/admin_panel.py` | 29 routes: admin panel pages under `/admin` — sign-in, labs, lab page, readiness, onboarding, providers, users, account |
| 497 | `api/access.py` | Access middleware: policy loading, coverage check, rate limits, caller identification |
| 594 | `api/access_policy.xml` | Every route: realm, allowed roles, rate limit, body cap (XML, not Python) |
| 492 | `api/routes/admin_api.py` | 32 routes: admin panel JSON under `/admin/api` — labs, steps, onboarding, lab users, autonomy, adaptation, platform users |
| 414 | `api/input_check.py` | Checks every request's parameters against the ones its route declares in the policy |
| 359 | `api/routes/onboarding.py` | 19 routes: lab-side onboarding — consents and clinical approvals |
| 299 | `api/routes/review.py` | 12 routes: queue, draft, revisions, signing, grading, feedback, audio |
| 393 | `api/ui.py` | The page frames and components every server-rendered page is built from |
| 282 | `auth/lab.py` | Lab users' sign-in: password check, access tokens, single-use refresh tokens |
| 233 | `admin/modelconfig.py` | Providers, model definitions, per-lab per-step assignment |
| 428 | `api/routes/review_ui.py` | 8 routes: browser sign-in, review screen, queue screen, lexicon screen, static assets |
| 227 | `admin/auth.py` | scrypt passwords, server-side sessions, revocation |
| 236 | `api/routes/admin_ops_panel.py` | 5 routes: the cost dashboard, the connection-pool page and the operational-settings pages |
| 171 | `api/routes/ga.py` | 6 routes: lab-side autonomy read/revoke, release coverage, HL7 + FHIR export, drift |
| 228 | `admin/onboarding_steps.py` | The onboarding uploads and mining steps an admin runs, shared by the pages and the API |
| 185 | `api/deps.py` | The caller the middleware identified, and one-lab session binding (primary or replica) |
| 24 | `api/routing.py` | `BridgedRoute`: every router's route class; wraps sync endpoints not marked `threaded` so they run bridged |
| 52 | `api/unavailable.py` | Answers 503 with `Retry-After` when the database or PgBouncer cannot be reached, instead of a bare 500 |
| 235 | `api/markdown.py` | Renders the project's own Markdown documents (FEATURES.md, API.md) to HTML for the public pages |
| 77 | `api/diagram.py` | The architecture diagram on the features page's system-design tab, as inline SVG in the page's colour tokens |
| 127 | `api/routes/health.py` | `/health`: liveness with per-check detail, and the instance id on every response |
| 211 | `api/routes/showcase.py` | 4 routes: the public pages — what the product does, its HTTP API, and the recruiter overview, plus their media |
| 39 | `api/routes/metrics.py` | 1 route: the Prometheus scrape, behind the metrics bearer token outside development |
| 118 | `admin/users.py` | Add, deactivate, reactivate and reset product admin and support accounts |
| 123 | `api/routes/ops.py` | 7 routes: query metrics, table health, PgBouncer pools, costs and operational settings as JSON |
| 113 | `api/routes/lexicon.py` | 7 routes: lab-side lexicon growth — new-term candidates and sound-alike variant review |
| 88 | `admin/cli.py` | Create the first admin, reset a password, revoke sessions |
| 159 | `api/app.py` | App factory, router wiring, middleware stack, `/ready` (dependencies) |
| 122 | `api/routes/ingest.py` | 3 routes: upload (validate, store, audit, queue the pipeline run) and the lab's recording list, and study registration by accession number |
| 85 | `api/pagination.py` | Pages for every list endpoint: bounded LIMIT/OFFSET, totals in response headers |
| 80 | `api/routes/auth.py` | 4 routes: lab sign-in, refresh, sign-out, change password |
| 44 | `api/read_your_writes.py` | Keeps a browser on the primary for a few seconds after it writes, so replica lag never hides its own change |

### `api/app.py` — the FastAPI application
Assembles the routers and puts the access check in front of all of them.

- `create_app()` mounts every router, runs `verify_coverage()` and `verify_params()`
  against the policy — the app refuses to start on a mismatch — and installs
  `AccessMiddleware` in front of `InputValidationMiddleware`, inside
  `RequestCacheMiddleware`, `ReadYourWritesMiddleware`, `QueryMetricsMiddleware`
  and `InstanceIdMiddleware`
- `/ready` — database connection and schema revision, on every shard when sharding
  is on; the shared cache and, when configured, the replica register their own checks
- `current_revision()` / `head_revision()` so a schema drift is visible at boot

### `api/routes/health.py` — liveness
- `health()` runs every registered check (database, pool, memory, plus any `register_check()` adds)
  and reports each one, but never fails the probe on a dependency blip
- `InstanceIdMiddleware` stamps every response with `instance_id()`, so an operator can tell which instance answered

### `api/pagination.py` and `api/read_your_writes.py`
- `Page.of()`, `paginate()` / `paginate_async()`, `set_page_headers()` — `X-Total-Count`, `X-Page`,
  `X-Page-Size`, `Link`. Bodies stay plain JSON arrays, so existing callers read page 1
- `ReadYourWritesMiddleware` — a write sets a marker cookie; a request carrying it, or
  `X-Read-Primary`, reads from the primary. Only active when a replica is configured

### `api/ui.py` — the shared page components
One design system for the admin panel and the lab screens.

- `esc()`, `icon()`; page frames `admin_page()`, `lab_page()`, `auth_page()`
- Components: `flash`, `card`, `stat`, `badge`, `table`, `empty`, `progress`, `steps`, `facts`, `line_chart`, `bar_list`

### `api/access_policy.xml` — who may call what
The single list of every route the app serves. Adding a role or a permission is
an edit here, not in code.

- `<roles>` — `product_admin` and `support` (admin realm); `lab_admin`,
  `radiologist`, `transcriptionist`, `auditor` (lab realm)
- `<rate-limits>` — named sliding-window limits, keyed by caller or by IP
- `<routes realm="public|admin|lab">` — method, path, `roles="a,b"`,
  `rate-limit`, `max-body-bytes`, and `environments` for the dev-only docs routes
- `<param>` per accepted parameter — `in` (path/query/form/file/json), `type`,
  `required`, `pattern`, `max-length`, `multiple`
- A role may only be allowed on a route of its own realm; the parser refuses the file otherwise

### `auth/lab.py` — lab users' sign-in
- `login()` — password check (same answer and timing for every failure), then a token pair
- `issue_access_token()` / `verify_access_token()` — HS256, 15 minutes, user id + tenant id + roles
- `refresh()` — single-use refresh tokens stored as hashes; a replayed one revokes its whole sign-in
- `logout()`, `revoke_all()`, `set_password()` (by a product admin), `change_password()` (by the user)
- `require_token_secret()` — the app refuses to start outside development without a 32+ character secret

### `api/routes/auth.py` — `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/password`

### `api/input_check.py` — the parameter allowlist
Runs right after the access middleware, so only for a caller already let through.

- `handler_params()` / `verify_params()` — read what each handler really accepts
  (path, query, form, file, JSON model fields) and refuse to start if the policy differs
- `InputValidationMiddleware` — unknown, repeated, malformed or missing parameter
  `400`; body counted while it is spooled (memory, then disk past 1 MiB) so a
  chunked body is capped too `413`; the handler gets the spooled body unchanged
- Errors name the parameter, never its value

### `api/access.py` — the access middleware
Every request passes through it before any handler runs, so a route cannot be
reached without a policy entry and a caller the policy allows.

- `parse_policy()` / `load_policy()` — validate the XML once at startup
- `verify_coverage()` — served routes and policy routes must match exactly
- `AccessMiddleware` — unlisted route `404`; declared body over the cap `413`;
  rate limit `429` with `Retry-After`; caller identification `401` (or a `303` to
  `/admin/login` for a signed-out browser on a panel page); role check `403`
- Admin realm: the `radreport_admin` session cookie. Lab realm: the placeholder
  bearer access token from `/auth/login`, verified without a database round trip
  and their stored roles checked
- `SharedRateLimiter` — a fixed-window counter in the unlogged `rate_limit_counter` table,
  used by every shipped limit so counts hold across workers; fails open if the table is
  unreachable. `RateLimiter` — an in-process sliding window, for a limit without `store="shared"`
- `AccessPolicy.allows()` — used by the panel to hide controls a role cannot use

### `api/deps.py` — request dependencies
Holds the boundary invariant: a request is bound to exactly one tenant before it
touches a tenant-scoped table, and a product admin binding to a lab writes an
audit row. Authentication itself happens in `api/access.py`.

- `current_admin()` / `current_principal()` — read the caller the middleware identified
- `get_db()` — a session bound to the lab user's own tenant; `get_read_db()` — the
  same, read-only and on the replica when one is usable
- `admin_lab_session()` / `get_admin_lab_db()` — a session bound to the
  `{tenant_id}` in the path, with `select_org()` writing `admin_org_selected`
- `client_ip()` for audit rows
- Each session dependency reads the matched route: for a bridged one it opens and closes the session in greenlets on the async engine, for a `threaded` one on worker threads with the sync engine; either way through the same admission gate of `pool_size + max_overflow` sessions

### `api/routing.py` and `api/unavailable.py` — how a request runs, and when the database is gone
- `BridgedRoute`, every router's `route_class`: wraps each sync endpoint not marked `threaded` so it runs bridged (`db/bridge.py`)
- `DatabaseUnavailableMiddleware`: a lost or refused connection, or no pooled connection in time, anywhere in the stack (the access check included) becomes `503` with `Retry-After: 5`; pinned with a real PgBouncer killed and restarted in `tests/db/test_pgbouncer_failover.py`

### `api/routes/ingest.py` — capture-only ingest
Upload → validate → store → `recording` row → audit, then a `run_pipeline` job
and a `recording.ingested` event in the same transaction. Nothing is transcribed
in the request; a worker does that, so the upload returns at once.

- `upload_recording()` returns `IngestResponse` with the queued job, idempotent on re-upload
  (the job's dedupe key is the recording id)
- `list_recordings()` pages through what the lab has captured, on the async read session

### `api/routes/onboarding.py` — lab-side onboarding
Only what the lab's own staff can do: the consents and the clinical approvals.
Uploads and mining steps moved to the admin panel.

- Voice enrollment and training consent
- Template candidate list and review, merge decisions, batch apply/revert
- Mapping verification, histogram, referrer prior; collision-finding resolution
- Verbatim queue and submission; boilerplate export and promotion
- Critical-rule authoring and approval — every clinical gate is a radiologist

### `api/routes/review.py` — the review API
Permissions loaded from the caller's `app_user` roles and built into a
`Reviewer`, never trusted from a header.

- Queue and stats, draft fetch, audio span fetch
- Revisions, signature, addendum, alert acknowledgement
- Grading, CSE rate, usefulness reporting
- `active_edit_seconds` arrives from the browser because only the browser can measure focus

### `api/routes/review_ui.py` — the review screen
Server-rendered. A JavaScript build chain in the path of every clinical review,
for a screen that is a form with a timer, is a dependency nobody needs.

- Browser sign-in for lab users: `login_page()`, `login_submit()` (httponly token
  cookies), `refresh_session()`, `logout_submit()`
- `review_screen()` and `queue_screen()`; `render_field()`, `render_retractions()`
- The lexicon screen at `/ui/lexicon` — new-term candidates and variant review
- `static_file()` serves the four assets with no build step; a request carrying the
  current `?v=` may be cached for a year

### `api/static/` — the client-side pieces
The only things the client genuinely must do, plus the shared stylesheet.

- `app.css` — the design system: tokens, the app shell and the components, light and dark
- `app.js` — the theme switch, the mobile navigation drawer, dismissable notices
- `review.js` — focus-time timer and per-field click-to-listen
- `review.css` — the review screen's own components, on top of `app.css`'s tokens

### `api/routes/admin_panel.py` — the admin panel's pages
Server-rendered, for the same reason as the review screen. Every action is a
form POST that redirects back with a URL-encoded `?error=` or `?notice=`.

- Sign in / out; the login page shows configured demo accounts on a test-credentials tab
- Lab list (offboarded hidden unless `?show=all`) and registration, with
  server-side slug validation and `training_consent_ref` required when pooling
  consent is ticked
- Lab page: status-change form offering only legal targets, per-step model
  proposal and *activate* buttons, a password reset per lab user; readiness page, bound to the lab's session
- Onboarding page: overview, roster, template, shorthand and corpus uploads, a button per mining step, merge proposals
- Providers and models; platform users; the signed-in admin's own account and password
- Controls a `support` account cannot use are hidden, by asking the access policy

### `api/routes/admin_ops_panel.py` — the admin panel's operations pages
- `costs_page()` — spend across labs, or one lab's by stage, with spikes marked (`monitoring/costs.py`)
- `pools_page()` — PgBouncer's pools, with waiting clients and busy pools flagged (`db/pgbouncer_stats.py`)
- `config_page()` — operational settings, platform-wide or for one lab; `set_config_value()` / `reset_config_value()`

### `api/routes/ops.py` — the operations JSON
- `GET /admin/api/ops/queries` (`db/instrumentation.py`), `GET /admin/api/ops/tables` (`db/table_health.py`), `GET /admin/api/ops/pgbouncer` (`db/pgbouncer_stats.py`)
- `GET /admin/api/costs` (`monitoring/costs.py`)
- `GET /admin/api/ops/config`, `POST .../config/{key}` and `.../config/{key}/reset` (`core/system_config.py`)

### `api/routes/lexicon.py` — lab-side lexicon growth
- `list_candidates()`, `scan_now()`, `approve_candidates()`, `reject_candidates()` over `onboarding/term_watch.py`
- `list_variants()`, `decide_variant()`, `variant_stats()` over `knowledge/variant_review.py`
- Approval is a radiologist's call; lab admins can see the queues

### `api/routes/admin_api.py` — the admin panel's JSON API
The same operations as the pages, for scripts and tests, under `/admin/api`.
Every lab-scoped route takes the lab from its `{tenant_id}` path segment.

- Labs: list, register, readiness, status change
- Models per step: steps view, propose, activate (gated on a gold-set eval run)
- Onboarding: status, batch list and batch status, roster, templates, shorthand, corpus, merge proposals, `steps/{step}`
- Lab users: list, set password
- Autonomy read, open accrual, grant, revoke; adaptation gates and require-gates
- Platform users: list, create, deactivate, reactivate, reset password; the admin's own password

### `api/routes/ga.py` — autonomy, export, drift
The lab side of the later-stage features. Opening accrual, granting and the
adaptation gates moved to the admin API, so the permission asymmetry is now a
realm boundary.

- `get_accrual()` and `post_revoke()` — a lab can watch and revoke, never grant
- `get_autonomy_coverage()` — what a grant actually removed
- `get_hl7()` / `get_fhir()` export, `get_drift()`

### `admin/auth.py` — product-admin authentication
Replaces the header placeholder that made anyone with a UUID a product admin.

- scrypt password hashing: `hash_password()`, `verify_password()`, `set_password()`
- `login()` / `authenticate()` / `logout()` over `admin_session`
- `authenticate_cached()` — the access middleware's check, through the shared cache
- `revoke_all_sessions()` for a compromised account

### `admin/users.py` — platform users
Lets a product admin manage who signs in to the panel without a shell.

- `list_platform_users()`, `create_platform_user()`, `set_active()`, `reset_password()`
- Refuses to deactivate yourself or the last active product admin
- Deactivation and password reset end every session; every change is audited

### `admin/onboarding_steps.py` — onboarding steps an admin runs
One implementation behind both the onboarding page and the admin API.

- `onboarding_overview()` — corpus verification, gold progress, active rules, recent batches, readiness
- `import_roster_file()`, `submit_template_files()`, `submit_shorthand_files()`,
  `load_corpus_records()` / `load_corpus_file()`, `propose_template_merges()`
- `batches_query()` and `batch_status()` for the batch list and one batch's contents
- `STEPS` — `derive-map`, `lexicon-mine`, `collision-audit`, `mine-variants`,
  `boilerplate-mine`, `critical-rules-seed`, `acceptance-assemble` / `acceptance-freeze`
  (fill and freeze the lab's acceptance set, which the pilot gate needs), and
  `radlex-annotate` (needs `BIOPORTAL_API_KEY`); `run_step()` runs one by name
- `StepRefused` carries the HTTP status the API answers with

### `admin/modelconfig.py` — per-lab model configuration
The missing half of the registry: the tables and resolver shipped, but nothing
except the seed script could write them.

- `create_provider()`, `create_definition()`, `propose_assignment()`
- `step_configuration()`, `available_models()`, `unconfigured_steps()`
- Cloud API keys stay in the process environment; `resolve_api_key()` reads them by reference

### `admin/cli.py` — bootstrap from a shell
Breaks the loop where signing in requires an account and creating one requires
signing in. The right place for it: whoever can run this already has the
database. Every later account is added from the panel's Users page.

- `create` (`make admin EMAIL=...`), `set-password` (`make admin-password EMAIL=...`),
  `revoke-sessions` — `python -m radreport.admin.cli`

---

## 9. Background work

Work that runs outside a request: the job queue and its workers, and the domain
events relayed after a commit. The loops are entry points, like Surfaces — they
call down and are called by nothing. What other modules use is the write side:
`enqueue()` and `emit()`, each inside the caller's own transaction, so a
rolled-back change neither queues work nor announces itself.

**13 files, 804 lines.** (2 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 142 | `workers/worker.py` | The worker loop: claim jobs, run each in a session bound to its own lab, and record what happened |
| 109 | `events/consumers.py` | Consumers of domain events, each applying an event at most once however often it is delivered |
| 97 | `workers/maintenance.py` | Platform jobs that keep the database healthy, run by the same workers as everything else |
| 108 | `events/bus.py` | Where relayed events go: one `EventBus` interface, with a Postgres and a Kafka implementation chosen by config; `kafka_client_config()` adds TLS and SASL for a hosted broker |
| 93 | `workers/queue.py` | The job queue's operations, on top of the `job` table and its `claim_jobs()` function |
| 52 | `events/relay.py` | Moves committed outbox events onto the bus, then marks them sent |
| 51 | `workers/handlers.py` | What each kind of job does; a handler gets a session already bound to the job's lab |
| 49 | `workers/__main__.py` | `python -m radreport.workers [--kinds run_pipeline,ensure_partitions] [--concurrency 2]` |
| 38 | `events/__main__.py` | `python -m radreport.events relay`, or `consume analytics` to drain one Kafka consumer |
| 32 | `workers/schedule.py` | Periodic platform jobs, queued once per period however many workers are running |
| 31 | `events/outbox.py` | Writing a domain event: one outbox row, in the same transaction as the change it reports |

### `workers/queue.py` — the job queue
A Postgres table, not a broker: a job commits or rolls back with the change that
queued it.

- `enqueue()` — inside the caller's transaction; a `dedupe_key` makes a repeat a no-op
- `claim()` calls `claim_jobs()` (migration 0013), which leases ready work across every lab with `FOR UPDATE SKIP LOCKED`
- `extend_lease()`, `complete()`, `fail()` — `fail()` retries with backoff until the attempts run out, then the job is dead
- `reap()` buries leases that ran out on their last attempt

### `workers/worker.py`, `handlers.py`, `__main__.py` — running jobs
- `Worker.run_once()` claims; `_Heartbeat` keeps the lease alive while a job runs
- `job_session()` binds the session to the job's lab; the handler's writes and the
  job's completion commit together. On an error the writes roll back and the failure
  is recorded in a fresh session
- `Worker.run_forever()` polls with backoff when the queue is empty and calls `schedule.enqueue_due()` about once a minute
- `handler(kind)` registers a handler; `load_all()` imports the modules that register the shipped kinds

| Kind | Registered in | Does |
|---|---|---|
| `run_pipeline` | `pipeline/runner.py` | One recording through the graph; queued by `POST /ingest/recordings` |
| `reap_jobs` | `workers/maintenance.py` | Every 10 minutes: bury expired leases on their last attempt |
| `ensure_partitions` | `workers/maintenance.py` | Every 6 hours: create monthly partitions ahead, detach months past retention |
| `cost_anomaly_scan` | `workers/maintenance.py` | Every 6 hours: emit new cost spikes as `cost.anomaly` events |
| `refresh_eval_set` | `workers/maintenance.py` | Daily: refresh the materialized canonical eval set |
| `watch_lexicon` | `workers/maintenance.py` | Daily: scan every lab's edits for new vocabulary (`onboarding/term_watch.py`) |

The periods are `PERIODIC` in `workers/schedule.py`; the period number in the
dedupe key keeps it to one job per period across workers.

### `events/` — the transactional outbox
- `outbox.py` — `emit()` writes one `outbox_event` row. Topics: `recording.ingested`,
  `draft.ready`, `report.signed`, `autonomy.revoked`, `cost.anomaly`
- `relay.py` — `Relay.run_once()` locks a batch of unsent events in commit order
  (`claim_outbox`, `FOR UPDATE SKIP LOCKED`), publishes them, and marks them sent in
  the same transaction. A crash between publishing and marking resends the batch
- `bus.py` — `PostgresEventBus` delivers to the in-process consumers; `KafkaEventBus`
  produces to topics keyed by lab; `get_bus()` picks one from settings
- `consumers.py` — `deliver()` applies an event to each subscriber in its own
  transaction bound to the event's lab, recording it in `consumed_event` in that
  same transaction, so each consumer applies an event once. Shipped consumers:
  `analytics` (one structured line per event) and `critical_alerts` (a notification
  for each draft that carries an urgent finding)

---

## Placeholders

Four directories exist in the tree and contain no code. They imply capability
that is not there; fill them or delete them.

| Path | Intended for |
|---|---|
| `radreport/prompts/` | prompt text as data — prompts are built in `adapters/llm/prompt.py` and the stages |
| `radreport/adapters/dicom/` | DICOM metadata lookup |
| `radreport/adapters/hl7/` | inbound HL7 (orders); outbound lives in `export/hl7.py` |
| `radreport/pipeline/stages/specialists/` | per-modality specialist stages |

---

# Summary tables

Counts from the filesystem on 2026-10-07. Package `__init__.py` stubs of three
lines or fewer are included in the totals but omitted from the per-module tables
above; the stub count is noted under each module heading.

## By module

Grouped by **when the code runs**, which is how the sections above are ordered.

| # | Module | Files | Lines | Share | Runs |
|---|---|---:|---:|---:|---|
| 1 | [Foundation](#1-foundation) | 90 | 9,922 | 30.9% | always |
| 2 | [Onboarding](#2-onboarding) | 15 | 3,259 | 10.1% | once per lab |
| 3 | [Capture](#3-capture) | 6 | 547 | 1.7% | per recording |
| 4 | [Pipeline](#4-pipeline) | 41 | 5,582 | 17.4% | per report |
| 5 | [Engines](#5-engines) | 16 | 1,542 | 4.8% | called by the pipeline |
| 6 | [Review and export](#6-review-and-export) | 10 | 1,301 | 4.0% | per draft, then per signature |
| 7 | [Governance](#7-governance) | 19 | 2,263 | 7.0% | out of band |
| 8 | [Surfaces](#8-surfaces) | 36 | 6,936 | 21.6% | per HTTP request |
| 9 | [Background work](#9-background-work) | 13 | 804 | 2.5% | per job, per event |
| | **Total** | **246** | **32,156** | | |

Foundation is now the largest: the 68-table schema and its 24 migrations, the
session, replica, sharding and instrumentation layers, the caches, and the
developer tools. The Surfaces follow — three realms of routes plus the access
layer that checks every request — then the Pipeline, its sixteen stages and the
lexicon knowledge they read. Capture is the smallest at 1.7% and does the least
on purpose — it validates, stores and audits; the upload route queues the
pipeline run, and nothing in Capture waits on it.

## By category

The same 246 files cut by **what a file is**, which is the cut that matters when
you are deciding where a change belongs rather than when it runs. The rule: `db/` is
DB / schema, `api/` is HTTP, `adapters/` is external I/O, `core/`, `cache/` and
`devtools/` and `observability/` are shared helpers, and everything else is domain logic.

| Category | Files | Lines | Share |
|---|---:|---:|---:|
| Domain logic | 111 | 14,764 | 45.9% |
| DB / schema | 56 | 5,390 | 16.8% |
| Controllers (HTTP) | 28 | 5,758 | 17.9% |
| Adapters (external I/O) | 18 | 1,713 | 5.3% |
| Helpers / shared | 33 | 4,531 | 14.1% |
| **Total** | **246** | **32,156** | |

Adapters are listed apart from domain logic because they talk to S3, an LLM or
an ASR engine. Counted as logic instead, that is **129 files and 16,477 lines**.
Two files outside `adapters/` also talk to an outside service:
`events/bus.py` (Kafka, counted as domain logic) and `cache/shared.py` (Redis,
counted as a helper).

`DB / schema` splits three ways, and the middle one is not editable code:

| Kind | Files | Lines | Editing rule |
|---|---:|---:|---|
| ORM models | 17 | 2,128 | Declare the 68 tables. Edit freely — but **any change here needs a new migration.** |
| Migrations | 24 | 1,733 | **Append-only history, not code.** See the table under [Foundation](#1-foundation). |
| Infrastructure | 15 | 1,529 | The machinery both rely on: `async_session.py`, `base.py`, `bootstrap.py`, `bridge.py`, `bulk.py`, `first_seed.py`, `instrumentation.py`, `introspect.py`, `env.py`, `pgbouncer_stats.py`, `session.py`, `sharding.py`, `shards.py`, `table_health.py`. |

## Data

**68 tables, 844 columns.**

| Tenancy class | Tables | Meaning |
|---|---:|---|
| Tenant-scoped | 47 | `tenant_id NOT NULL`, RLS policy with `FORCE`, composite FKs to other scoped tables |
| Tenant-NULLable | 15 | NULL = global: model catalog, global lexicon, canonical eval sets, audit log, platform jobs and events, platform-wide settings |
| No tenant | 6 | The platform realm: `tenant`, `platform_user`, `admin_session`, `rate_limit_counter`, `lab_shard`, `consumed_event` |

A new table on neither exception list and with no `tenant_id` **fails the
build** — `core/tenancy.py` declares the lists and
`tests/unit/test_schema_tenancy.py` enforces them.

| Area | Tables | Declared in |
|---|---:|---|
| Onboarding | 10 | `db/models/onboarding.py` |
| Knowledge | 10 | `db/models/knowledge.py` |
| Reports | 8 | `db/models/reporting.py` |
| Tenancy | 6 | `db/models/tenancy.py` |
| Identity | 5 | `db/models/identity.py` |
| ASR | 4 | `db/models/asr.py` |
| Review | 4 | `db/models/review.py` |
| Eval | 4 | `db/models/evaluation.py` |
| Model config | 4 | `db/models/modelconfig.py` |
| Orchestration | 3 | `db/models/orchestration.py` |
| Adaptation | 3 | `db/models/adaptation.py` |
| Events | 2 | `db/models/events.py` |
| Ops | 2 | `db/models/ops.py` |
| Ingestion | 1 | `db/models/ingestion.py` |
| Jobs | 1 | `db/models/jobs.py` |
| LLM cache | 1 | `db/models/llm_cache.py` |

Widest tables, which is where the detail lives: `recording` 27 columns (every
§9.8 quality measurement plus the §10.4 consent-derivation inputs),
`stage_execution` 21 (per-stage cost, tokens, cache hits, resolved model id),
`study` 19, `autonomy_class` 19, `model_adaptation_run` 18,
`template_version` 18.

Five tables are partitioned. Four by month because they grow per-event rather
than per-report: `asr_segment`, `edit_event`, `audit_log` (migration 0003) and
`stage_execution` (0015). `recording` is split into eight hash partitions on
`tenant_id` (0015). The `ensure_partitions` job keeps the monthly ones ahead of
the calendar, and the app role cannot read a partition directly.

## HTTP routes

**143 routes: 137 across 13 route files, `/health` and `/ready`, plus FastAPI's
four documentation routes.** Every one of them lives in [Surfaces](#8-surfaces) —
it is the only module that speaks HTTP — and every one is listed in
`api/access_policy.xml`, which `create_app()` checks at startup.
[API.md](API.md#appendix-a--index-by-prefix) has the roles, rate limit and body
cap of each.

| Routes | File | Surface |
|---:|---|---|
| 32 | `api/routes/admin_api.py` | Admin panel JSON: labs, steps, onboarding, lab users, autonomy, adaptation, platform users |
| 29 | `api/routes/admin_panel.py` | Admin panel pages and form handlers (**renders HTML**) |
| 19 | `api/routes/onboarding.py` | Lab-side onboarding: consents and clinical approvals |
| 12 | `api/routes/review.py` | Queue, draft, revisions, signing, grading, feedback, audio |
| 8 | `api/routes/review_ui.py` | Browser sign-in, review screen, queue screen, lexicon screen, static assets (**renders HTML**) |
| 7 | `api/routes/lexicon.py` | Lab-side lexicon growth: new-term candidates, variant review |
| 6 | `api/routes/ga.py` | Lab-side autonomy read/revoke, release coverage, HL7 + FHIR export, drift |
| 7 | `api/routes/ops.py` | Operations JSON: query metrics, table health, PgBouncer pools, costs, operational settings |
| 5 | `api/routes/admin_ops_panel.py` | Cost dashboard, connection-pool page and operational-settings pages (**renders HTML**) |
| 4 | `api/routes/auth.py` | Lab sign-in, refresh, sign-out, change password |
| 3 | `api/routes/ingest.py` | Upload (queues the pipeline run), study registration by accession number, and the lab's recording list |
| 4 | `api/routes/showcase.py` | The public pages: features, API reference, recruiter overview, and their media |
| 1 | `api/routes/metrics.py` | The Prometheus scrape |
| 1 | `api/routes/health.py` | `/health` (liveness, with each dependency's state) |
| 1 | `api/app.py` | `/ready` (connection + schema revision) |
| 4 | FastAPI | `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` — local, test and development only |

`/health` returning 200 against an unreachable database was a real bug; `/health`
now reports each dependency's state without failing the probe, and `/ready`
checks the connection **and** that the schema revision matches the code's head,
answering 503 otherwise. `make run` waits on `/ready`.

### By the module behind them

The same 127 application routes, attributed to the module whose behaviour each
one calls down into rather than to the file it sits in. This is the view to read
when tracing a request. Most admin operations are served twice — a page and a
JSON route — and both are counted.

| Module | Routes | Prefix | File |
|---|---:|---|---|
| [1 Foundation](#1-foundation) — query metrics, table health, operational settings | 8 | `/admin/config`, `/admin/api/ops` | `routes/admin_ops_panel.py`, `routes/ops.py` |
| [2 Onboarding](#2-onboarding) — lab registration and lifecycle | 7 | `/admin`, `/admin/api` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [2 Onboarding](#2-onboarding) — uploads, batches, mining steps, overview, readiness | 18 | `/admin/.../onboarding`, `/admin/api/.../onboarding` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [2 Onboarding](#2-onboarding) — lab-side approvals | 17 | `/onboarding` | `routes/onboarding.py` |
| [2 Onboarding](#2-onboarding) — lexicon growth and variant review, lab side | 8 | `/lexicon`, `/ui/lexicon` | `routes/lexicon.py`, `routes/review_ui.py` |
| [3 Capture](#3-capture) | 2 | `/ingest` | `routes/ingest.py` |
| [4 Pipeline](#4-pipeline) | **0** | — | queued by `POST /ingest/recordings`, run by a worker |
| [5 Engines](#5-engines) — providers, models, per-step assignment | 8 | `/admin`, `/admin/api` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [6 Review](#6-review-and-export) — API | 12 | `/review` | `routes/review.py` |
| [6 Review](#6-review-and-export) — screens and assets | 3 | `/ui` | `routes/review_ui.py` |
| [6 Export](#6-review-and-export) — HL7 + FHIR | 2 | `/ga/export` | `routes/ga.py` |
| [7 Governance](#7-governance) — autonomy and drift, lab side | 4 | `/ga` | `routes/ga.py` |
| [7 Governance](#7-governance) — autonomy and adaptation, admin side | 6 | `/admin/api/labs/{tenant_id}` | `routes/admin_api.py` |
| [7 Governance](#7-governance) — costs | 2 | `/admin/costs`, `/admin/api/costs` | `routes/admin_ops_panel.py`, `routes/ops.py` |
| [8 Surfaces](#8-surfaces) — admin sign-in, platform users, lab user passwords, own account | 20 | `/admin`, `/admin/api` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [8 Surfaces](#8-surfaces) — lab sign-in | 8 | `/auth`, `/ui` | `routes/auth.py`, `routes/review_ui.py` |
| [8 Surfaces](#8-surfaces) — ops | 2 | — | `routes/health.py`, `app.py` |

**The pipeline has no route of its own.** `POST /ingest/recordings` queues a
`run_pipeline` job in the same transaction as the `recording` row, and
`python -m radreport.workers` claims it and calls `pipeline/runner.py`, which
builds the graph with `build_v1_graph()` and opens the run with `new_run()`. Trace
the pipeline from `tests/db/test_job_queue.py` (ingest → worker → pipeline) or
`tests/db/test_pipeline_v1.py`, not from a request handler.

**40 of the 127 render HTML or redirect** rather than return JSON: the admin
panel's pages and form handlers (33, three of them public sign-in routes) and
the seven `/ui` screens and browser sign-in routes (four of them public). They
are listed again under [UI](#ui).

### Every route

Grouped by prefix, in source order within each file. Who may call each one is
in [API.md](API.md#appendix-a--index-by-prefix).

#### `/admin` — 29, the admin panel's pages (**HTML**)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/login` | Sign-in page (public) |
| POST | `/admin/login` | scrypt check, server-side session (public, 5 a minute per IP) |
| POST | `/admin/logout` | Revoke the session (public) |
| GET | `/admin` | Redirect to the lab list |
| GET | `/admin/labs` | Lab list and the onboard-a-lab form; `?show=all` includes offboarded labs |
| POST | `/admin/labs` | Register a lab |
| GET | `/admin/labs/{tenant_id}` | One lab: status, all pipeline steps and what serves each, its users |
| POST | `/admin/labs/{tenant_id}/users/{user_id}/password` | Set a lab user's password |
| POST | `/admin/labs/{tenant_id}/status` | Lifecycle transition, through the readiness gate |
| GET | `/admin/labs/{tenant_id}/readiness` | The seven checks gating onboarding → pilot |
| POST | `/admin/labs/{tenant_id}/assign` | Propose a model for one step |
| POST | `/admin/labs/{tenant_id}/assignments/{assignment_id}/activate` | Activate a proposal — refused without a gold-set eval run |
| GET | `/admin/providers` | Providers and models, with add forms |
| POST | `/admin/providers` | Add a provider |
| POST | `/admin/models` | Add a model definition |
| GET | `/admin/labs/{tenant_id}/onboarding` | Onboarding overview, uploads, step buttons |
| POST | `/admin/labs/{tenant_id}/onboarding/roster` | Import the roster CSV |
| POST | `/admin/labs/{tenant_id}/onboarding/templates` | Submit template documents |
| POST | `/admin/labs/{tenant_id}/onboarding/shorthand` | Shorthand reference sheets into the lab's lexicon |
| POST | `/admin/labs/{tenant_id}/onboarding/corpus` | Load the historical report corpus from a CSV or JSON file |
| POST | `/admin/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | Propose near-duplicate merges |
| POST | `/admin/labs/{tenant_id}/onboarding/steps/{step}` | Run one mining or seeding step |
| GET | `/admin/users` | Platform users, with add, deactivate, reactivate and password forms |
| POST | `/admin/users` | Add a product admin or support account |
| POST | `/admin/users/{user_id}/deactivate` | Switch an account off and end its sessions |
| POST | `/admin/users/{user_id}/reactivate` | Switch an account back on |
| POST | `/admin/users/{user_id}/password` | Set a password and end the account's sessions |
| GET | `/admin/account` | The signed-in admin's own account |
| POST | `/admin/account/password` | Change your own password; needs the current one |

#### `/admin` — 4, the operations pages (**HTML**)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/costs` | Spend per lab and per stage, with spikes marked |
| GET | `/admin/config` | Operational settings and where each value comes from |
| POST | `/admin/config/{key}` | Set one, platform-wide or for one lab |
| POST | `/admin/config/{key}/reset` | Remove a stored value so the next level applies |

#### `/admin/api` — 30, the admin panel's JSON

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/api/labs` | List labs, every status |
| POST | `/admin/api/labs` | Register a lab |
| GET | `/admin/api/labs/{tenant_id}/readiness` | The readiness gate as JSON |
| POST | `/admin/api/labs/{tenant_id}/status` | Lifecycle transition: onboarding → pilot → live |
| GET | `/admin/api/labs/{tenant_id}/steps` | Each pipeline step and what serves it |
| POST | `/admin/api/labs/{tenant_id}/assignments` | Propose a model for one step |
| POST | `/admin/api/labs/{tenant_id}/assignments/{assignment_id}/activate` | Activate a proposal — refused without a gold-set eval run |
| GET | `/admin/api/labs/{tenant_id}/onboarding` | Onboarding status across every stage |
| GET | `/admin/api/labs/{tenant_id}/onboarding/batches` | A lab's import batches, newest first, paged |
| GET | `/admin/api/labs/{tenant_id}/onboarding/batches/{batch_id}` | One import batch's status and contents |
| POST | `/admin/api/labs/{tenant_id}/onboarding/roster` | Import the roster CSV |
| POST | `/admin/api/labs/{tenant_id}/onboarding/templates` | Submit template documents |
| POST | `/admin/api/labs/{tenant_id}/onboarding/shorthand` | Shorthand reference sheets into the lab's lexicon |
| POST | `/admin/api/labs/{tenant_id}/onboarding/corpus` | Load the historical report corpus |
| POST | `/admin/api/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | Propose near-duplicate merges |
| POST | `/admin/api/labs/{tenant_id}/onboarding/steps/{step}` | Run `derive-map`, `lexicon-mine`, `collision-audit`, `mine-variants`, `boilerplate-mine`, `critical-rules-seed`, `acceptance-assemble`, `acceptance-freeze` or `radlex-annotate` |
| GET | `/admin/api/labs/{tenant_id}/users` | The lab's users |
| POST | `/admin/api/labs/{tenant_id}/users/{user_id}/password` | Set a lab user's password |
| GET | `/admin/api/labs/{tenant_id}/autonomy/{class_code}` | Accrual state and the Beta-Binomial posterior |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/open-accrual` | Start observing a class |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/grant` | The Bayesian sequential grant |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/revoke` | Withdraw autonomy from the vendor side |
| POST | `/admin/api/labs/{tenant_id}/adaptation/gates` | Evaluate the six adaptation gates |
| POST | `/admin/api/labs/{tenant_id}/adaptation/require-gates` | Enforce them — two are unimplemented and fail closed |
| GET | `/admin/api/users` | List platform users |
| POST | `/admin/api/users` | Add a product admin or support account |
| POST | `/admin/api/users/{user_id}/deactivate` | Switch an account off and end its sessions |
| POST | `/admin/api/users/{user_id}/reactivate` | Switch an account back on |
| POST | `/admin/api/users/{user_id}/password` | Set a password and end the account's sessions |
| POST | `/admin/api/account/password` | Change your own password; needs the current one |

#### `/admin/api/ops` and `/admin/api/costs` — 6, operations JSON

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/api/ops/queries` | Statement timings and statements per request, for this worker |
| GET | `/admin/api/ops/tables` | Per-table dead rows, vacuum history and size, with bloat flagged |
| GET | `/admin/api/costs` | Spend over 7, 30 or 90 days, across labs or for one lab by stage |
| GET | `/admin/api/ops/config` | Operational settings, effective values and their source |
| POST | `/admin/api/ops/config/{key}` | Set an operational setting, platform-wide or for one lab |
| POST | `/admin/api/ops/config/{key}/reset` | Remove a stored setting so the next level applies |

#### `/auth` — 4, lab sign-in

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | Lab user sign-in; returns an access token and a refresh token (public) |
| POST | `/auth/refresh` | Trade a refresh token for a new pair (public) |
| POST | `/auth/logout` | End the sign-in a refresh token belongs to (public) |
| POST | `/auth/password` | Replace your own password; ends every sign-in |

#### `/onboarding` — 17, lab-side onboarding

| Method | Path | Stage | Purpose |
|---|---|---|---|
| POST | `/onboarding/radiologists/{radiologist_id}/voice-enrollment` | S0 | Enroll a voice sample |
| POST | `/onboarding/radiologists/{radiologist_id}/training-consent` | S0 | Record training consent — separate from the clinical one |
| GET | `/onboarding/templates/candidates` | S1 | List parsed candidates |
| POST | `/onboarding/templates/candidates/{candidate_id}/review` | S1 | Accept or reject one candidate |
| POST | `/onboarding/merge-proposals/{proposal_id}/decide` | S1 | Decide one merge |
| POST | `/onboarding/batches/{batch_id}/apply` | — | Promote an approved batch |
| POST | `/onboarding/batches/{batch_id}/revert` | — | Revert a batch |
| POST | `/onboarding/corpus/mappings/{mapping_id}/verify` | S2 | Verify one derived mapping |
| GET | `/onboarding/corpus/histogram` | S2 | Usage histogram |
| GET | `/onboarding/corpus/referrer-prior` | S2 | Referrer prior |
| POST | `/onboarding/collision-findings/{finding_id}/resolve` | S3 | Resolve one collision |
| GET | `/onboarding/verbatim/queue` | S4 | The verbatim annotation queue |
| POST | `/onboarding/verbatim` | S4 | Submit a verbatim transcript |
| GET | `/onboarding/boilerplate/export` | S5 | CSV export |
| POST | `/onboarding/boilerplate/{candidate_id}/promote` | S5 | Promote one normal |
| POST | `/onboarding/critical-rules` | S6 | Author a rule |
| POST | `/onboarding/critical-rules/{rule_id}/approve` | S6 | Approve a rule |

The roster, template and corpus uploads, merge proposals, the mining and
seeding steps and the onboarding status used to be here; they are now admin
routes under `/admin/api/labs/{tenant_id}/onboarding`.

#### `/ingest` — 2, capture

| Method | Path | Purpose |
|---|---|---|
| POST | `/ingest/recordings` | Upload: §9.8 gates, store, audit, queue the `run_pipeline` job. Idempotent by content hash. |
| GET | `/ingest/recordings` | The lab's recordings, newest first, paged |

#### `/review` — 12, the human loop

| Method | Path | Purpose |
|---|---|---|
| GET | `/review/queue` | The queue: priority → alert → flagged count → oldest, role-filtered |
| GET | `/review/queue/stats` | Counts for the header |
| GET | `/review/drafts/{draft_id}` | One draft: fields with provenance |
| GET | `/review/drafts/{draft_id}/audio` | The audio, for click-to-listen |
| POST | `/review/drafts/{draft_id}/revisions` | Record a revision and its categorised edit events |
| POST | `/review/drafts/{draft_id}/sign` | Sign — the four refusals stand here |
| POST | `/review/reports/{report_id}/addendum` | Amend a signed report |
| POST | `/review/alerts/{alert_id}/acknowledge` | Acknowledge a critical alert |
| POST | `/review/reports/{report_id}/grade` | G0–G4 |
| GET | `/review/metrics/cse-rate` | CSE rate |
| POST | `/review/drafts/{draft_id}/usefulness` | §9.6's "this draft was useless" |
| GET | `/review/metrics/usefulness` | Usefulness rollup |

#### `/ui` — 8, browser sign-in and the lab screens (**HTML**)

| Method | Path | Purpose |
|---|---|---|
| GET | `/ui/login` | Lab user browser sign-in form (public) |
| POST | `/ui/login` | Browser sign-in; sets httponly token cookies (public) |
| GET | `/ui/refresh` | Renew the browser's token cookies from its refresh cookie (public) |
| POST | `/ui/logout` | End the browser's sign-in (public) |
| GET | `/ui/static/{name}` | `app.css`, `app.js`, `review.css`, `review.js` (public) |
| GET | `/ui/drafts/{draft_id}` | Review screen |
| GET | `/ui/queue` | Queue screen |
| GET | `/ui/lexicon` | New-terms and sound-alike review screen |

#### `/lexicon` — 7, lab-side lexicon growth

| Method | Path | Purpose |
|---|---|---|
| GET | `/lexicon/candidates` | Terms radiologists use that the lexicon lacks, waiting for review |
| POST | `/lexicon/scan` | Scan the edits made since the last scan now |
| POST | `/lexicon/candidates/approve` | Approve terms into a new lexicon version |
| POST | `/lexicon/candidates/reject` | Set terms aside |
| GET | `/lexicon/variants` | Sound-alike matches waiting for an answer, or automatic approvals to spot-check |
| POST | `/lexicon/variants/{variant_id}/decide` | Answer whether a heard phrase means the term |
| GET | `/lexicon/variants/stats` | Automatic approvals and overrides per threshold arm |

#### `/ga` — 6, lab-side governance and export

| Method | Path | Purpose |
|---|---|---|
| GET | `/ga/autonomy/{class_code}` | Accrual state and the Beta-Binomial posterior |
| POST | `/ga/autonomy/{class_code}/revoke` | Withdraw autonomy from the lab side |
| GET | `/ga/autonomy-coverage` | Coverage: what a grant actually changed |
| GET | `/ga/export/{report_id}/hl7` | HL7 v2 ORU^R01, MLLP-framed |
| GET | `/ga/export/{report_id}/fhir` | FHIR R4 DiagnosticReport + transaction bundle |
| GET | `/ga/drift` | PSI against an explicit baseline window |

Opening accrual, granting and the adaptation gates moved to `/admin/api`.

#### Ops — 2

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, with each dependency's state and this instance's id |
| GET | `/ready` | Connection **and** schema revision matches the code's head; 503 routes traffic away |

## UI

**2,421 lines, 8 files.** Server-rendered HTML with no build step: no
`package.json`, no bundler, no `node_modules`.

| Lines | File | Purpose |
|---:|---|---|
| 778 | `api/routes/admin_panel.py` | Admin panel: 8 pages (sign-in included), 20 form handlers and the `/admin` redirect |
| 428 | `api/routes/review_ui.py` | Lab screens: browser sign-in, review screen, queue screen, lexicon screen |
| 393 | `api/ui.py` | Page frames and components shared by every page |
| 236 | `api/routes/admin_ops_panel.py` | Admin panel: the cost dashboard, the connection-pool page and the operational-settings page, with 2 form handlers |
| 429 | `api/static/app.css` | The design system: tokens, app shell, components, light and dark |
| 102 | `api/static/review.js` | Focus timer (`active_edit_seconds`), click-to-listen, edit collection |
| 84 | `api/static/app.js` | Theme switch, mobile navigation drawer, dismissable notices |
| 55 | `api/static/review.css` | The review screen's own components, on `app.css`'s tokens |

The four Python files are also counted under Surfaces above — they are Python
that emits HTML, not a separate tree.

| Route | Screen |
|---|---|
| `GET /admin/login` | Sign in, with a test-credentials tab when demo accounts are configured |
| `GET /admin/labs` | Lab list + onboard-a-lab form |
| `GET /admin/labs/{tenant_id}` | One lab: status change, all pipeline steps, propose and activate a model for each, lab user passwords |
| `GET /admin/labs/{tenant_id}/readiness` | The readiness checks, blocking first |
| `GET /admin/labs/{tenant_id}/onboarding` | Onboarding overview, roster, template, shorthand and corpus uploads, step buttons |
| `GET /admin/providers` | Providers and models, with add forms |
| `GET /admin/users` | Platform users, with add, deactivate, reactivate and password forms |
| `GET /admin/account` | Your own account and password |
| `GET /admin/costs` | Spend per lab and per stage, with spikes marked |
| `GET /admin/config` | Operational settings, platform-wide or per lab |
| `GET /ui/login` | Lab user sign-in |
| `GET /ui/queue` | Review queue |
| `GET /ui/drafts/{draft_id}` | Review a draft |
| `GET /ui/lexicon` | New terms and sound-alike matches waiting for a radiologist |

Plus the admin panel's form POST handlers, each of which redirects back to a
page, the browser sign-in POST and refresh routes, and the `/ui/static/{name}`
asset route.

The browser JavaScript exists for the two things a server cannot do: measure
**focus time** (blur/focus events plus a 20-second idle timeout — §15.2's
commercial argument rests on that number) and **play a cited audio span** from
`provenance_span.audio_start_ms`. `app.js` only handles the theme, the mobile
drawer and notices. Everything else is a form POST and a redirect.

## Tests

**111 files, 11,906 lines, 810 test functions** (counted as `def test_`, so a
parametrized test counts once). That is 27% of the repository's lines against
73% application code.

| Directory | Files | Lines | Tests | What runs there |
|---|---:|---:|---:|---|
| `tests/unit/` | 54 | 5,536 | 515 | No database; runs anywhere |
| `tests/db/` | 52 | 6,110 | 287 | Against a real Postgres, as the app role |
| `tests/integration/` | 1 | 118 | 8 | The ingest API end to end |
| `tests/fixtures/` | 2 | 23 | 0 | A minimal text-PDF writer for the shorthand tests |
| `tests/` | 2 | 119 | 0 | `conftest.py` and package marker |

Files added since the last count, by the module they cover:

| Module | Files (tests) |
|---|---|
| [1 Foundation](#1-foundation) — sessions, pool, replica, sharding | `db/test_async_session.py` (5), `unit/test_db_pool.py` (3), `db/test_pgbouncer.py` (1), `db/test_overload.py` (1), `db/test_read_replica.py` (6), `db/test_sharding.py` (3), `unit/test_hash_ring.py` (4), `db/test_bridge.py` (5), `db/test_pgbouncer_failover.py` (2), `unit/test_pgbouncer_stats.py` (4) |
| [1 Foundation](#1-foundation) — instrumentation, query and table health | `db/test_query_instrumentation.py` (3), `unit/test_query_instrumentation.py` (4), `db/test_query_counts.py` (5), `db/test_query_report.py` (2), `db/test_vacuum_and_compression.py` (5), `db/test_partition_maintenance.py` (7), `db/test_bulk_import.py` (4) |
| [1 Foundation](#1-foundation) — caches, settings, devtools | `unit/test_request_cache.py` (7), `db/test_shared_cache.py` (5), `unit/test_shared_cache.py` (4), `db/test_bloom_filters.py` (4), `unit/test_bloom.py` (4), `db/test_system_config.py` (7), `unit/test_local_accounts.py` (3), `unit/test_demo_lab.py` (2), `unit/test_observability.py` (10), `db/test_backlog_metrics.py` (2) |
| [2 Onboarding](#2-onboarding) | `db/test_shorthand_import.py` (3), `unit/test_shorthand.py` (6), `db/test_template_fallback.py` (2), `unit/test_template_llm.py` (3), `db/test_term_watch.py` (2), `unit/test_term_watch.py` (3) |
| [3 Capture](#3-capture) | `db/test_register_study.py` (1), `unit/test_local_object_store.py` (2) |
| [4 Pipeline](#4-pipeline) — pipeline and knowledge | `db/test_pipeline_write_batching.py` (1), `db/test_term_lookup.py` (1), `unit/test_synonyms.py` (5), `db/test_languages.py` (1), `unit/test_languages.py` (6), `db/test_variant_review.py` (3), `unit/test_variant_review.py` (2), `db/test_training_export.py` (1) |
| [5 Engines](#5-engines) | `db/test_llm_response_cache.py` (7), `unit/test_stub_asr_text.py` (1) |
| [7 Governance](#7-governance) | `db/test_cost_dashboard.py` (7), `unit/test_cost_spikes.py` (5), `db/test_autonomy_classes.py` (1) |
| [8 Surfaces](#8-surfaces) | `db/test_pagination.py` (5), `db/test_caching_headers.py` (4), `db/test_ui_pages.py` (6), `db/test_write_routes_fail_cleanly.py` (1), `unit/test_features_page.py` (9), `unit/test_markdown.py` (3) |
| [9 Background work](#9-background-work) | `db/test_job_queue.py` (8), `db/test_outbox.py` (7), `unit/test_kafka_config.py` (3) |

Plus three helpers with no tests: `db/helpers.py` (a platform account and a
signed-in panel client), `fixtures/__init__.py` and `fixtures/pdf.py`.

DB-backed tests skip unless `RADREPORT_TEST_DATABASE_URL` is set, so the unit
suite runs anywhere. They must connect as the **non-owner** `radreport_app_login`
role: a superuser or table owner bypasses RLS, and the isolation tests — the
highest-value tests in the suite — would pass without proving anything.
