# TODO Roadmap (2026-10-05)

**Status as of 2026-10-07.** Each item below has a status line: **Done** (ticked), **Partly done**
(what is left is named) or **Blocked** (what it waits for). Most of what is left needs production
traffic, pilot labs, a model server or hosted infrastructure, not code.

## Migrations: every step must be guarded

Fixed 2026-10-07: `alembic upgrade head` now runs from an empty database and from
a database stopped at 0005, and the full test suite passes on both as the
non-owner role. Revision 0001 still builds the schema from the **live models**, so
on a fresh database later revisions find their changes already applied. The rule
that keeps this working: **every step in a new migration checks the database
first** (`_has_column`, `_has_table`, `_has_constraint`, `_has_index`, `_has_policy`
in 0004–0007 are the pattern), and a constraint named in full is wrapped in
`op.f(...)` so the naming convention does not prefix it a second time.

---

---

# Cost Optimization Roadmap (2026-10-05)

## Overview
App not yet cost-optimized. Upgrading hardware alone will only help 10-30%.
Expected optimization potential: **40-60% cost reduction** with app-level fixes.

**ROI:** $20k engineering → $108-144k savings over 3 years (5-7x return)

---

## PHASE 1: Quick Wins (1-2 weeks, save $1-2k/month)

### Priority: CRITICAL

- [x] **Add Query Instrumentation** (4 hours)
  - **Status: Done.** `db/instrumentation.py` counts and times every statement per request and logs those over `db.slow_query_ms` (100 ms); SQL echo is on in the test environment; `make pg-observe` sets `log_min_duration_statement = 100` and loads `pg_stat_statements`; p50/p95/p99 and per-route counts at `/admin/api/ops/queries`.
  - Enable SQLAlchemy echo in test mode
  - Add slow-query logging (queries > 100ms)
  - Set up Postgres `log_min_duration_statement = 100`
  - Add metrics: query count, query time percentiles
  - Integration: `radreport/db/session.py`

- [x] **Increase Connection Pool** (1 hour)
  - **Status: Done.** 30 + 10 overflow per worker, from settings (`RADREPORT_DB__POOL_SIZE`). Load-tested at 500 concurrent users (PERFORMANCE_BASELINE.md): direct to Postgres two workers exhaust `max_connections = 100`, so the app now warns at start-up and production should run behind PgBouncer. Postgres memory was watched on a laptop only, not on production hardware.
  - Change default from `pool_size=10` to `pool_size=30-50`
  - Test under peak load (500 concurrent users)
  - Monitor Postgres memory usage
  - File: `radreport/db/session.py` line 42

- [x] **Add Request-Scoped Caching** (3 hours)
  - **Status: Done.** `cache/request.py`, opened per request in `api/deps.py`: lab config, model assignments and user roles are read once per request.
  - Cache tenant config per request
  - Cache model assignments per request
  - Cache user roles per request
  - Use FastAPI dependency with cache lifetime
  - Location: `radreport/api/deps.py` (new request context manager)

- [ ] **Identify Missing Indexes** (2 hours)
  - **Status: Partly done.** `devtools/query_report.py` reads `pg_stat_statements`, explains the top 20 and lists unindexed foreign keys; findings in DB_INDEXES_TODO.md and the indexes added in migration 0011 (incl. `pipeline_run (tenant_id, created_at)` and `stage_execution (tenant_id, task_key, created_at)`). Run against the test-suite workload because there is no production database yet; rerun on production.
  - Run `SELECT * FROM pg_stat_statements` on production
  - EXPLAIN ANALYZE top 20 slowest queries
  - Document findings in `DB_INDEXES_TODO.md`
  - Likely candidates:
    - `study.patient_id` (without tenant_id prefix)
    - `pipeline_run.created_at` (for cost queries)
    - `stage_execution.task_key` (for metrics)

---

## PHASE 2: N+1 Query Elimination (2-3 weeks, save $2-4k/month)

### Priority: HIGH

- [x] **Fix Admin Panel Queries** (4 hours)
  - **Status: Done.** The models have no `branding`/`users`/`sessions` relationships to eager-load, so each page was rewritten to joins and grouped counts instead: `/admin/labs` 11 → 6 statements, lab detail 17 → 10, readiness 19 → 13. Pinned by `tests/db/test_query_counts.py`.
  - Location: `radreport/api/routes/admin_panel.py`, `radreport/api/routes/admin_api.py`
  - Add `selectinload(Tenant.branding)` on lab list
  - Add `selectinload(Tenant.users)` on lab detail
  - Add `joinedload(PlatformUser.sessions)` on user admin
  - Test: compare query counts before/after

- [x] **Fix Report Generation Queries** (3 hours)
  - **Status: Done.** Stages read one per-run knowledge snapshot rather than ORM relationships, so there was nothing to `joinedload`; the run's stage traces and domain rows are buffered into one batched flush: 37 → 8 statements per 15-stage run (`test_pipeline_write_batching.py`).
  - Location: `radreport/pipeline/stages/`
  - Add `joinedload(Recording.study)` in extraction stage
  - Add `joinedload(Recording.metadata)` early
  - Add `selectinload(Study.findings)` for verification stage
  - Profile cost reduction

- [x] **Fix Onboarding List Queries** (3 hours)
  - **Status: Done.** Batches and candidates are paged in the database, a batch status endpoint was added, and the onboarding overview reuses the readiness counts (23 → 17 statements).
  - Location: `radreport/api/routes/onboarding.py`
  - Add eager loading for ImportBatch relationships
  - Add pagination (LIMIT/OFFSET)
  - Fix: batch status endpoint

- [x] **Implement Batch Insert Operations** (4 hours)
  - **Status: Done.** `db/bulk.py` inserts in 1,000-row batches with client-side ids; 5,000 corpus reports load in 13 statements and 0.18 s (`test_bulk_import.py`); rosters and mined terms use it too.
  - Location: `radreport/onboarding/` (import handlers)
  - Replace per-row inserts with `bulk_insert_mappings()`
  - Batch size: 1000 rows per batch
  - Expected: 10-50x faster for large imports
  - Test with 5000-row onboarding corpus

- [x] **Implement Batch Updates** (2 hours)
  - **Status: Done.** Pipeline writes are buffered and flushed once per run; timings in PERFORMANCE_BASELINE.md.
  - Location: `radreport/pipeline/` (pipeline writes)
  - Add batch flush for pending_writes
  - Use `bulk_save_objects()` for stage_execution updates
  - Profile: compare 100-row flush timing

- [x] **Add Query Result Pagination** (2 hours)
  - **Status: Done.** `api/pagination.py`: default page size 20, maximum 100, `X-Total-Count` and `Link` headers, on every list endpoint including `/admin/labs`, `/admin/users` and `/ingest/recordings` (there is no `/api/studies` route).
  - Location: `radreport/api/routes/` (all list endpoints)
  - Add LIMIT/OFFSET to all list queries
  - Document: default page_size=20, max=100
  - Endpoints to audit:
    - GET /admin/labs
    - GET /admin/users
    - GET /api/studies
    - GET /api/recordings

---

## PHASE 3: Infrastructure (3-4 weeks, save $1-3k/month + 3-5x capacity)

### Priority: HIGH

- [x] **Deploy PgBouncer** (4 hours)
  - **Status: Partly done.** Transaction-mode config in `ops/pgbouncer/`, `make pgbouncer`, a docker-compose service, and `RADREPORT_DB__PGBOUNCER=true` turns off prepared statements. `tests/db/test_pgbouncer.py` shows 200 clients sharing ≤ 25 server connections and that lab binding never leaks between them. Pool numbers are on a dashboard: `/admin/pools` (and `/admin/api/ops/pgbouncer`) reads `SHOW POOLS` and `SHOW CONFIG` from the admin console, with clients waiting and pools at 80% or more of `default_pool_size` flagged. Failover is tested: `tests/db/test_pgbouncer_failover.py` starts its own PgBouncer, SIGKILLs it mid-run and restarts it — requests in between answer `503` with `Retry-After: 5` within the connect timeout (`api/unavailable.py`, the access check included), and both the async and the sync engine recover without an app restart (`pool_pre_ping`). A managed pooler's own failover (two PgBouncers behind a load balancer) is a deployment concern, not tested here.
  - Set up connection pooler in front of Postgres
  - Config: `pool_mode = transaction`
  - Expected: 10x more app connections, same DB load
  - Test failover scenarios
  - Monitoring: bounce counts, pool utilization

- [ ] **Add Read Replicas** (2-3 days)
  - **Status: Partly done.** Routing is built: `read_session()` sends admin pages, the cost dashboard, lists and exports to `RADREPORT_DB__REPLICA_URL`, falls back to the primary when the replica lags or is down, and a browser that just wrote reads the primary (read-your-writes cookie). Writes and the review screen stay on the primary. No replica has been provisioned, so it was tested with the primary standing in for one.
  - Create read replica of primary Postgres
  - Add read/write routing in app
  - Routes for read replicas:
    - Admin panel queries
    - Report view endpoints
    - Metering/cost queries
  - File: `radreport/db/session.py` (add read_replica_session())
  - Keep writes on primary (necessary for RLS)

- [ ] **Partition Large Tables** (3-4 days)
  - **Status: Partly done.** `recording` is hash-partitioned by lab (8 partitions) and `stage_execution` by month (migration 0015), with direct partition access revoked so RLS cannot be bypassed. `pipeline_run` was not partitioned: a dozen tables reference it, and its row volume is small next to `stage_execution`, which holds a row per stage. There is no `training_corpus_item` table in the schema. Query and DELETE performance on production-sized data is not measured.
  - Partition `pipeline_run` by month (created_at)
  - Partition `recording` by tenant_id
  - Partition `training_corpus_item` by tenant_id
  - Create migration (alembic)
  - Test: query performance, DELETE performance

- [x] **Enable VACUUM Tuning** (1 hour)
  - **Status: Done.** Migration 0016 sets aggressive autovacuum on the high-churn tables and every partition; new monthly partitions copy the settings. Bloat is estimated by `db/table_health.py` and shown at `/admin/api/ops/tables` (no `pg_bloat_check` dependency).
  - Increase autovacuum on high-churn tables
  - Set aggressive for: `pipeline_run`, `stage_execution`, `recording`
  - Monitor bloat with `pg_bloat_check`

- [ ] **Enable Column Compression** (2 hours)
  - **Status: Partly done.** Migration 0016 uses lz4 (faster than pglz; pglz where the server lacks lz4) on the large text and JSON columns; it applies to new writes. The storage report shows the ratio per column, but the 50–70% reduction has not been measured on real data.
  - Add COMPRESSION=pglz to large text columns
  - Columns: transcript, json_data, audio_metadata
  - Expected: 50-70% storage reduction
  - Slight query slowdown (acceptable tradeoff)

---

## PHASE 4: Advanced (Optional, long-term)

### Priority: MEDIUM

- [x] **Async Database Driver** (1-2 weeks)
  - **Status: Done.** Every sync route now runs **bridged** (`db/bridge.py`, `api/routing.py`): on the event loop inside a SQLAlchemy greenlet — the mechanism `AsyncSession` is built on — so its queries go through psycopg's asyncio driver and a request waiting on the database holds no worker thread. The route bodies and domain modules stay synchronous; inside the bridge every session factory hands out the async engine. Blocking work that touches no session (S3, local disk, Redis, RadLex, Google Translate, scrypt, audio decoding) is offloaded to a thread. The access middleware's lookups are bridged too. Deliberately kept on worker threads (`@threaded`, the sync engine): the onboarding uploads, steps and merge proposals, which run seconds of CPU between queries and would stall every other request on the loop. Pinned by `tests/db/test_bridge.py`. Measured (PERFORMANCE_BASELINE.md): the mechanism gives 4.3x the requests per second on a 20 ms query (`async_bench`); on the real app on one laptop, CPU-bound, throughput is unchanged with a local database and about 5% higher with no errors (sync: 2% errors) through a 2 ms-latency proxy. The gain grows with database latency; a like-for-like production measurement is still to do.
  - Migrate to `sqlalchemy[asyncio]` or `asyncpg`
  - Change: sync Session → async AsyncSession
  - Expected: 20-30% more req/sec capacity
  - Large refactor; schedule separately

- [x] **LLM Response Caching** (1 week)
  - **Status: Done.** `adapters/llm/response_cache.py`: keyed on the full request, Postgres jsonb by default or Redis, per lab, with a TTL; k samples stay k distinct answers.
  - Cache LLM responses keyed on (stage, input_hash)
  - Backend: Redis or Postgres jsonb
  - Expected: 5-15% cost reduction for repeat submissions
  - Location: `radreport/adapters/llm/base.py`

- [x] **Materialized Views** (3 hours)
  - **Status: Done.** `mv_canonical_eval_set` (migration 0017), refreshed daily by the worker's `refresh_eval_set` job.
  - Create MATERIALIZED VIEW for `v_canonical_eval_set`
  - Refresh daily at low-traffic time
  - Expected: 5% improvement for eval-heavy workloads

- [x] **Per-Lab Cost Dashboard** (2-3 hours)
  - **Status: Done.** `/admin/costs`: spend per lab and day with a trend chart, per-stage breakdown, and spike alerts from the `cost_anomaly_scan` job (also published as a `cost.anomaly` event). It lives in `admin_ops_panel.py`; there is no `admin_ui.py`.
  - Expose `v_tenant_metering_rollup` in admin UI
  - Add cost trend chart
  - Add cost/stage breakdown
  - Add anomaly detection (cost spike alerts)
  - Location: `radreport/api/routes/admin_ui.py`

---

## Metrics to Track

### Before Optimization
- [ ] Document baseline:
  - **Status: Partly done.** Statements per request, statement p50/p95/p99 and the 500-user load test are in PERFORMANCE_BASELINE.md, measured locally. DB CPU under peak, monthly query volume and the real monthly cost need production.
  - Query count per request (trace random 100 requests)
  - Average query time (p50, p95, p99)
  - DB CPU usage under peak load
  - Monthly query volume
  - Current cost: $10k/month (assumed)

### After Each Phase
- [ ] Phase 1 complete:
  - **Status: Partly done.** Instrumentation, pool size and the slow-query analysis are done (on the test workload). Savings cannot be measured without a production bill.
  - Query instrumentation set up
  - Slow query log analyzed
  - Connection pool increased
  - Estimated cost savings: $1-2k/month

- [x] Phase 2 complete:
  - **Status: Done.** N+1 reads removed and batch writes in place; statements per request fell 30–80% (PERFORMANCE_BASELINE.md). Savings not measurable yet.
  - N+1 queries eliminated
  - Batch operations in place
  - Query count per request reduced by ~50%
  - Estimated additional savings: $2-4k/month

- [ ] Phase 3 complete:
  - **Status: Partly done.** PgBouncer runs locally, two large tables are partitioned, and 4 workers behind PgBouncer serve 500 users at 99.99% success. Read replicas are not active, and a 3–5x throughput figure needs a like-for-like production comparison.
  - PgBouncer deployed
  - Read replicas active
  - Large tables partitioned
  - Throughput increased 3-5x
  - Estimated additional savings: $1-3k/month

---

## Validation Tests

- [x] Query instrumentation working: check slow query log for entries — **Done:** slow statements are logged and counted (`tests/db/test_query_instrumentation.py`).
- [x] Connection pool increased: verify `SHOW max_connections` and actual conn count — **Done:** pool size from settings; the app reads `SHOW max_connections` at start-up and warns when the pools could exceed it.
- [x] Request caching working: profile request handler, cache hit rate > 80% — **Done:** hit rate above 80% on steady admin traffic (`tests/db/test_shared_cache.py`).
- [x] N+1 queries fixed: query count per request < 10 (was > 50 before) — **Done:** no page grows with the number of rows, and every page is under 10. The seven readiness checks now judge facts read in one statement (`readiness.load_facts`), so the lab readiness page is 5 statements (was 13) and the onboarding overview 7 (was 17); pinned in `tests/db/test_query_counts.py`.
- [x] Batch operations fast: 5000-row import < 5 seconds (was ~50s) — **Done:** 5,000 rows in 0.18 s.
- [x] PgBouncer working: app handles 2x connections with same DB load — **Done:** 200 clients on ≤ 25 server connections.
- [ ] Read replicas working: 90% read queries hit replica (via query tagging) — **Blocked:** reads are tagged by target (`reads_by_target` at `/admin/api/ops/queries`), but there is no replica to measure the share against.

---

## Cost Projection

| Phase | Effort | Monthly Savings | Cumulative |
|-------|--------|-----------------|-----------|
| Phase 1 | 1-2 weeks | $1-2k | $1-2k (10-20% off) |
| Phase 1+2 | 3-4 weeks | $2-4k | $3-6k (30-60% off) |
| Phase 1+2+3 | 6-8 weeks | $1-3k | $4-9k (40-90% off) |
| Phase 1+2+3+4 | 8-12 weeks | varies | $5-9k (50-90% off) |

**Hardware upgrade ROI:** Only worthwhile AFTER phases 1-3 (else wasted on inefficient queries)

---

## Notes

- Monitor Postgres under production load after each phase
- Use `pg_stat_statements` extension for query analysis
- Set up alerting on: slow queries, connection pool exhaustion, cache miss rate
- Document all query optimization decisions in commit messages
- Test each change with at least 2x expected load

---

# Lexicon & Term Extraction Roadmap (2026-10-05)

## PHASE 0 (Current)

- ✅ Regex-based term extraction from structured templates
- ✅ Levenshtein + Double-Metaphone fuzzy matching
- ✅ Radiologist approval for all variants
- ✅ Single PDF upload (templates only)
- ✅ S3/S4/S3-Pass2 loop with manual approval

---

## PHASE 1 (Post-Launch) — High Priority

### 1. Lightweight LLM for Unstructured Template Extraction

**Why:** Current regex handles structured PDFs (95% of templates), but fails on free-form, handwritten, or poorly formatted documents.

**Implementation Strategy:**
```
Current (Phase 0):
  Template PDF → Regex → 60% extraction on unstructured

Phase 1:
  Template PDF → Regex (fast path)
    ├─ confidence > 0.8? → Use regex result ✅
    └─ confidence < 0.8? → Send to LLM fallback
      ↓
    LLM (lightweight) → 90% extraction on unstructured
```

**Why NOT Claude?**
- Claude: $0.01–0.03 per document (expensive at scale)
- Overkill for simple term extraction
- 3–5 sec response time (poor UX)
- Rate limits on enterprise plans

**Why Lightweight LLM (Qwen, Gemini Nano)?**
- Qwen 2.5 7B: $0.0001 per call (100x cheaper than Claude!)
- Gemini Nano: Free on-device (Google Cloud)
- Latency: 200–500ms (acceptable UX)
- Accuracy: 88–92% (good enough + radiologist review)
- Can self-host (no vendor lock-in)

**Cost Comparison:**
```
Phase 0: 20 templates/month × $0.00 (regex) = $0.00

Phase 1A (Claude fallback):  20 × $0.01 = $0.20
Phase 1B (Qwen fallback):    20 × $0.0001 = $0.002  ✅ 100x cheaper!
Phase 1C (Gemini Nano):      20 × $0.00 = $0.00 (free)
```

**Accuracy Improvement:**
```
Regex only (Phase 0):     60–95% depending on format
Regex + Qwen (Phase 1):   92–98% ← 7% improvement on hard cases
Regex + Claude (Phase 1): 95–99% ← 4% improvement at 100x cost
```

**Recommendation:** Start with **Qwen 2.5 7B** (self-host) or **Gemini Nano** (Google Cloud).

**TODO:**
- [ ] Evaluate Qwen 2.5 7B vs Gemini Nano (cost/accuracy/latency) — **Partly done:** compared on paper in docs/TEMPLATE_MODEL.md: Gemini Nano runs only on-device (Chrome, Android) and has no server API, so Qwen 2.5 7B on an OpenAI-compatible server is the path. No side-by-side cost/latency measurement, because no model server was available.
- [x] Implement regex + Qwen fallback for unstructured templates — **Done:** `onboarding/template_llm.py`; the model only adds fields whose labels appear in the document, failures keep the parser's result, and model-read templates always get a field-by-field review.
- [ ] Set up Qwen self-hosting or Gemini Nano integration — **Partly done:** steps for Ollama and vLLM are in docs/TEMPLATE_MODEL.md and the lab's `template_parse` step takes any OpenAI-compatible endpoint; no server has been stood up.
- [ ] Test with 3 labs (Lab A, B, C) — **Blocked:** needs pilot labs' templates.
- [x] Add confidence thresholds (< 0.8 → LLM) — **Done:** `templates.llm_fallback_below`, 0.80 by default, per lab in System settings.
- [ ] Measure: accuracy before/after — **Partly done:** `python -m radreport.devtools.template_eval` on 8 fixture templates: parser alone 31% recall (0% on the five unstructured formats), 100% precision. The "after" number needs a real model run.

---

### 2. Support Shorthand Reference PDF Upload

**Current Problem:** Shorthands often in separate reference documents for transcriptionists.

**Implementation:**
```
Phase 0 (Current):
  Upload: template.pdf only
  Result: S4 ASR biased on formal terms only

Phase 1 (Proposed):
  Upload: template.pdf + shorthand_reference.pdf
  Parser extracts:
    ├─ Template → "Left Lower Lobe", "Heart Size"
    └─ Reference → "LLL" → "Left Lower Lobe"
  Merge: keyterm_set = {formal names} + {shorthand → formal}
  Result: S4 ASR biased on BOTH formal + shorthand
```

**Impact on ASR Accuracy:**
```
Without shorthand:  Radiologist: "LLL shows PNA" → Whisper: "eel ell ell shows pee-en-ay" ❌ (40% error)
With shorthand:     Same audio → Whisper (biased): "LLL shows PNA" ✅ (5% error)
Improvement:        ~35% on shorthand-heavy dicts
```

**UI Changes:**
```
S3 Upload Form:
  ├─ [Upload Template PDFs] ✅ (required)
  └─ [Upload Shorthand Reference PDFs] (optional)
       └─ Parser auto-extracts shorthand mappings
```

**Shorthand Extraction Patterns:**
```
Pattern: "LLL = Left Lower Lobe"
Pattern: "RLL → Right Lower Lobe"
Pattern: "PNA | Pneumonia"
Pattern: "RUL/LUL/RLL/LLL" (with context lookup)
```

**TODO:**
- [x] Add file upload field for shorthand PDFs on the admin panel's onboarding page — **Done:** on the onboarding page (`/admin/labs/{id}/onboarding`), PDF, Word, text or CSV.
- [x] Create shorthand extraction regex patterns (5 patterns minimum) — **Done:** seven patterns (`=`, arrows, pipes, colon, dash, aligned columns, slash groups) in `onboarding/shorthand.py`.
- [x] Merge shorthand_map into keyterm_set before S4 — **Done:** mappings become lexicon terms with short forms, which `build_keyterms` biases ASR toward.
- [x] Test with 5 different hospital reference formats — **Done:** five formats plus a PDF in `tests/unit/test_shorthand.py`; they are synthetic, not real hospital sheets.
- [x] Add audit trail for shorthand source → keyterm mapping — **Done:** `shorthand_mapped` and `shorthand_conflict` audit entries name the file and line.

---

### 3. Semantic Matching for Synonym Terms

**Current Problem:** "consolidation" vs "infiltrate" = same meaning, but fuzzy match fails.

**Implementation:**
```
Phase 0:
  "consolidation" vs "infiltrate" → confidence: 0.0 ❌

Phase 1:
  "consolidation" vs "infiltrate"
    ↓
  Check medical synonym database (RadLex or SNOMED CT)
    ↓
  Match found: Both = "SNOMED:39607008" (lung consolidation)
    ↓
  Confidence: 0.95 ✅
```

**Tools:**
- Use **RadLex API** (free, standard in radiology)
- Or local **SNOMED CT** UMLS database
- Or lightweight query: "Are these medical synonyms?"

**Accuracy Boost:** +4–6% on synonym-heavy templates

**TODO:**
- [x] Integrate RadLex API for term lookup — **Done:** `knowledge/synonyms.RadLexClient` via BioPortal, used by the onboarding step *Look terms up in RadLex*; needs `BIOPORTAL_API_KEY`.
- [ ] Build local SNOMED CT concept mapper — **Partly done:** a curated local concept set (`knowledge/data/synonyms.csv`, 43 concepts) maps synonyms offline. SNOMED CT itself needs a UMLS licence, so no SNOMED codes are stored.
- [x] Test on known synonym pairs (50+ pairs) — **Done:** 164 pairs.
- [x] Document synonyms used for future updates — **Done:** docs/SYNONYMS.md.

---

### 4. Auto-Detect Unrecognized Terms During Live Usage

**User Feedback:** YES! Brilliant idea — continuous lexicon evolution.

**Implementation:**
```
Every radiologist edit → edit_event created

Check: Is term in lab-a-lexicon?

If NO:
  ├─ Log to potential_lexicon_term table
  └─ Track frequency

Weekly: Show radiologist approval UI:
  ├─ "You used 'ground glass opacity' 5 times"
  ├─ "Not in your lexicon — approve adding it?"
  └─ Radiologist: ☑️ YES
     ↓
     Create lexicon_set version 2 (auto)
```

**New Table:**
```sql
CREATE TABLE potential_lexicon_term (
  id UUID PRIMARY KEY,
  tenant_id UUID FK(tenant.id),
  surface_text TEXT,
  frequency INT,
  first_seen_at TIMESTAMP,
  last_seen_at TIMESTAMP,
  approved BOOLEAN DEFAULT false,
  approved_at TIMESTAMP,
  approved_by UUID FK(app_user.id),
  lexicon_set_version_id UUID (after approval)
);
```

**Radiologist Dashboard:**
```
"Approve New Terms for Lab A"
┌─────────────────────────────────────────┐
│ Term                    │ Frequency │ Act │
├─────────────────────────────────────────┤
│ ground glass opacity    │ 5         │ ✅  │
│ mosaic perfusion        │ 3         │ ✅  │
│ crazy paving pattern    │ 2         │ ✅  │
│ unusual finding (typo?) │ 1         │ ❌  │
└─────────────────────────────────────────┘
```

**Benefit:** Lexicon evolves with real-world usage (continuous improvement).

**TODO:**
- [x] Create potential_lexicon_term table — **Done:** migration 0020.
- [x] Add background job: aggregate edit_events → find new terms — **Done:** `watch_lexicon`, daily, with a per-lab watermark (`onboarding/term_watch.py`).
- [x] Build a lab-side radiologist approval UI (the approvals stay out of the admin panel) — **Done:** `/ui/lexicon` ("New terms"), radiologists approve and lab admins can see it.
- [x] Auto-create lexicon_set version on approval — **Done:** `term_watch.approve` (behind `POST /lexicon/candidates/approve`) adds the approved terms to the lab's lexicon as its next version (`new_version`), links each term to that version and audits it as `lexicon_terms_approved`; `tests/db/test_term_watch.py`.
- [ ] Test workflow with Lab A (1 month of live data) — **Blocked:** needs a live pilot lab.

---

### 5. Confidence Score Thresholds & Auto-Approval

**Current:** All matches shown to radiologist (low-confidence clutter).

**Implementation:**
```
Phase 0:
  confidence > 0.0 → Show to radiologist ❌ (cluttered)

Phase 1:
  confidence > 0.85 → Auto-approve ✅
  0.60–0.85 → Show to radiologist (review only)
  < 0.60 → Hide (too risky)
  
  UI: "Unsure about 'tree and bud' ↔ 'tree-in-bud'?"
      [Confidence: 0.72]
      ☐ Yes, same  ☐ No, different  ☐ Unsure
```

**Impact:** Reduces radiologist burden from 100% review to ~20% review.

**TODO:**
- [x] Define confidence thresholds per term type — **Done:** `lexicon.auto_approve_above` (0.85), stricter 0.92 for abbreviations and code words, `lexicon.review_above` (0.60); all settable per lab.
- [ ] A/B test thresholds with radiologist — **Partly done:** `lexicon.ab_test` splits labs into arms by id and `/lexicon/variants/stats` compares override rates per arm; the experiment has not been run with radiologists.
- [x] Auto-approve high-confidence matches (log for audit) — **Done:** each one is audited (`lexicon_variant_auto_approved`) and listed on `/ui/lexicon` for spot checks.
- [x] Track radiologist overrides (improve algorithm) — **Done:** reversing an automatic approval is logged as `lexicon_variant_override` and counted per arm.

---

### 6. Multi-Language Support for Medical Terms

**Current:** English only.
**Problem:** Lab A is in India; mix of English + Hindi medical terms.

**Implementation:**
```
Phase 1:
  Support: English, Hindi, French, Spanish
  
  Hindi example:
    "फेफड़े की सूजन" (pneumonia in Hindi)
      ↓
    Normalize to English: "pneumonia"
      ↓
    Link to English lexicon
  
  Use: Google Translate API or local Indic models
```

**TODO:**
- [x] Support Hindi term input (Latin script + native script) — **Done:** Devanagari, and Hinglish (`languages.hi_latin`, opt-in) — `knowledge/languages.py`, docs/LANGUAGES.md.
- [x] Add translation layer (Hindi → English) — **Done:** bundled dictionary, used by term lookup and by the pipeline's normalise stage; optional Google Translate for single unknown words, off by default.
- [ ] Test with Lab A Hindi-speaking radiologists — **Blocked:** needs the pilot lab; the dictionary also needs review by a Hindi-speaking radiologist.
- [x] Support 3–5 languages based on customer demand — **Done:** Hindi, French and Spanish; adding one is a CSV and a setting.

---

## PHASE 2 (Future) — Lower Priority

### 7. Active Learning: Fine-Tune Custom Extraction Models

```
Use data from Phase 1 radiologist approvals
  ↓
Build custom Qwen fine-tune on YOUR templates
  ↓
Extraction accuracy improves to 96–98%
  ↓
Deploy self-hosted Qwen for all labs

Cost: One-time $500 fine-tune, then $0.00 per call
Accuracy: 96–98% (vs 88–92% base Qwen)
```

**TODO:**
- [ ] Collect Phase 1 radiologist approval data — **Partly done:** the export is built (`python -m radreport.devtools.training_export`, docs/FINE_TUNING.md), only from labs that turn on `training.share_approvals`; there is no live approval data yet.
- [ ] Fine-tune Qwen 7B on collected templates — **Blocked:** needs the data above and a GPU.
- [ ] Test fine-tuned model vs base Qwen — **Blocked:** procedure in docs/FINE_TUNING.md; depends on the fine-tune.
- [ ] Deploy self-hosted fine-tuned Qwen — **Blocked:** depends on the fine-tune.
- [ ] Measure accuracy improvement — **Blocked:** depends on the fine-tune.

---

## Accuracy Projections

```
┌─────────────────────┬──────┬──────┬──────┬─────────┐
│ Approach            │ P0   │ P1   │ P2   │ Comments│
├─────────────────────┼──────┼──────┼──────┼─────────┤
│ Regex only          │ 85%  │ -    │ -    │ Base    │
│ + Qwen fallback     │ -    │ 92%  │ -    │ Cheap   │
│ + RadLex synonyms   │ -    │ +4%  │ -    │ Medical │
│ + Fine-tuned Qwen   │ -    │ -    │ 98%  │ Custom  │
│ Final (all stacked) │ 85%  │ 96%  │ 98%  │ Best    │
└─────────────────────┴──────┴──────┴──────┴─────────┘
```

---

## Whisper ASR Reliability Dependencies

**Question:** Does reliability depend ONLY on audio quality?

**Answer:** ~70% audio quality + ~30% other factors.

**Breakdown:**
```
1. AUDIO QUALITY (70%)
   ├─ Noise floor
   ├─ Clarity of speech
   ├─ Background sounds
   └─ Codec quality (FLAC > MP3)

2. SPEAKER FACTORS (15%)
   ├─ Accent (Indian English: 90%, Native: 95%)
   ├─ Pace (fast dictation: lower accuracy)
   ├─ Fatigue (end of day: 85% vs 92% morning)
   └─ Medical jargon familiarity

3. DOMAIN FACTORS (10%)
   ├─ Keyterm_set quality (good vocab: 92% vs bad: 85%)
   ├─ Whisper model version (large-v3 > base)
   └─ Temperature setting (0.0 = deterministic)

4. SYSTEM FACTORS (5%)
   ├─ Beam search width
   ├─ Decoding strategy
   └─ Language model weighting
```

**Example:**
```
Studio audio + native speaker + good keyterm_set:
  Whisper accuracy: 98% ✅

Hospital PA system + diverse accents + minimal keyterm_set:
  Whisper accuracy: 70% ❌

CONCLUSION: Audio quality important, but NOT the only factor.
Keyterm_set quality and speaker proficiency matter significantly.
```

---

## Cost Impact Summary

```
Current (Phase 0):  $0 (all regex + radiologist)
Phase 1 estimate:   +$2–3K upfront, then $0.0001 per template
Phase 2 estimate:   +$500 fine-tune, then $0 per template (self-hosted)

Payback: 2–3 labs × 20 templates/quarter × 10 quarters = 600 templates
  Phase 0 cost:  $0
  Phase 1 cost:  $2K + $0.06 = $2.06
  Phase 2 cost:  $2.5K + $0 = $2.5K (break-even by month 12)

Radiologist time saved per lab per year: 10 hours = $2–3K value
ROI: Positive by month 6–9 ✅
```

---

## Configuration & DevOps (Before Production)

### Priority: HIGH

- [x] **Move G3_speaker_balance limits to configurable settings** (4 hours)
  - **Status: Done.** `system_config` table (migration 0012) with platform and per-lab values, edited on the admin panel's System settings page; `evaluate_gates()` reads them.
  - Current: Hardcoded in [adaptation/gates.py:47-54](radreport/adaptation/gates.py#L47-L54)
    - `GLOBAL_ADAPTER_HOURS = 20.0` 
    - `MIN_SPEAKERS = 5`
    - `MAX_SPEAKER_SHARE = 0.40`
  - **Approach:** Create `system_config` table (or use the admin panel)
  - Admin can adjust thresholds without code changes
  - Environment variable fallback: `ADAPTER_HOURS_THRESHOLD`, `MIN_SPEAKERS_FOR_ADAPTER`, `MAX_SPEAKER_SHARE`
  - Integration: Update `evaluate_gates()` to read from config instead of constants
  - **Why:** Pilot may need different thresholds; ops should control without engineering

- [ ] **Add health check endpoint + multi-instance LB support** (6 hours)
  - **Status: Partly done.** `GET /health` returns status, per-check results with latency (database, connection pool, memory, the shared cache, and the replica when one is configured; `/ready` also checks every shard), the instance id (`INSTANCE_ID` or generated) and an `x-instance-id` header, without writing to the database. Not yet verified against a real load balancer.
  - **Endpoint:** `GET /health` returns JSON `{status: "healthy", checks: {db: true, redis: true, ...}}`
  - **Purpose:** Allow load balancer to route away from unhealthy instances
  - **Design for scale:**
    - Use dependency injection for instance ID: `INSTANCE_ID` env var or UUID header
    - Health check should NOT require database write (read-only, fast)
    - Return per-check status (e.g., db latency, memory usage)
  - **Integration:** `radreport/api/routes/health.py` (new file)
  - **Before production:** Verify with LB that can consume this endpoint
  - **Future:** Add metrics (response time, error rate) to health check

- [x] **Add environment variables for adapter gates** (2 hours)
  - **Status: Done.** `ADAPTER_HOURS_THRESHOLD`, `MIN_SPEAKERS_FOR_ADAPTER`, `MAX_SPEAKER_SHARE` (and the other gate values) are the fallback under the stored settings.
  - Current hardcoded constants in [adaptation/gates.py:47-54](radreport/adaptation/gates.py#L47-L54)
  - Add env var fallbacks (with defaults):
    - `ADAPTER_HOURS_THRESHOLD=20.0` (G1_volume gate)
    - `MIN_SPEAKERS_FOR_ADAPTER=5` (G3_speaker_balance gate)
    - `MAX_SPEAKER_SHARE=0.40` (G3_speaker_balance gate)
  - Integration: Update `evaluate_gates()` to read from env at startup
  - **Why:** Allows ops to tune thresholds for different labs/pilot stages without code changes
  - Example usage: `ADAPTER_HOURS_THRESHOLD=15.0 python -m radreport.main` (lab with less data)

---

# Scale & System Design Roadmap (2026-10-06)

What exists today: monthly partitions on `asr_segment`, `edit_event` and
`audit_log` (migration 0003), Anthropic prompt caching, and in-process
`lru_cache`s. Everything below is missing. Ordered by value for effort; the
queue and outbox come first because the crash test depends on them.

## Easy — fits the product, no paid infrastructure

- [x] **Postgres job queue** (1 day)
  - **Status: Done.** `job` table and `claim_jobs` with `FOR UPDATE SKIP LOCKED` (migration 0013); uploads enqueue `run_pipeline`; visibility timeout, attempts and dead-letter; `radreport/workers/`; tests show two workers never share a job and a killed worker's job is reclaimed.
  - New `job` table; workers claim with `SELECT ... FOR UPDATE SKIP LOCKED`
  - Upload enqueues a `run_pipeline` job instead of running it in the request
  - Visibility timeout + attempt count + dead-letter state for poison jobs
  - Fill the empty `radreport/workers/` package with the worker loop
  - Jobs are tenant-scoped: the worker must `bind_tenant` before touching rows
  - Test: two workers never claim the same job; a killed worker's job is reclaimed

- [x] **Transactional outbox** (half day)
  - **Status: Done.** `outbox_event` written in the change's transaction (migration 0014), relay with per-consumer dedupe; the crash-between-commit-and-publish test delivers exactly once per consumer.
  - New `outbox_event` table written in the same transaction as the change
  - Events: `recording.ingested`, `draft.ready`, `report.signed`, `autonomy.revoked`
  - A relay publishes unsent rows and marks them sent; consumers must be idempotent
  - Test: crash between commit and publish → event still delivered exactly once per consumer

- [x] **Redis cache** (1 day)
  - **Status: Done.** `cache/shared.py`: lab config, model assignments and admin sessions with TTLs and after-commit invalidation; memory fallback without `RADREPORT_REDIS_URL`; every key carries the lab id. The Upstash instance for the hosted demo has not been created.
  - Cache tenant config, model assignments and admin sessions; TTL + explicit invalidation on write
  - In-memory fallback when `RADREPORT_REDIS_URL` is unset, so local dev and tests need no Redis
  - Upstash free tier for the hosted demo
  - Cache keys must include `tenant_id` — a missing one is a cross-lab leak that RLS cannot catch
  - Builds on "Add Request-Scoped Caching" and "LLM Response Caching" above

- [x] **Bloom filter** (half day)
  - **Status: Done.** `knowledge/bloom.py`, used for duplicate recordings and unknown terms; each lab's filter is built from the database on first use (not at start-up) and rebuilt as it ages; tests check no false negatives and the false-positive rate.
  - Pure Python, no dependency; sized from expected items + target false-positive rate
  - Uses: duplicate-recording pre-check on `content_hash` before the DB lookup; unknown-term pre-check in lexicon matching
  - A "maybe" still goes to the database — the filter only skips work on a definite "no"
  - Per tenant, rebuilt on startup from the DB
  - Test: no false negatives; measured false-positive rate within target

- [ ] **Extend partitioning** (1 day)
  - **Status: Partly done.** The bug is fixed: `ensure_partitions` runs every 6 hours keeping 3 months ahead and can split rows out of a default partition; old months are detached by `detach_month_partitions` per the retention settings; `recording` is partitioned by lab. `stage_execution` was partitioned by month instead of `pipeline_run` (see "Partition Large Tables").
  - **Bug:** 0003 creates partitions 12 months ahead at migration time and nothing
    creates more. After that, rows fall into `<table>_default`, pruning stops, and
    creating that month's partition later fails because the default holds its rows.
  - Add a scheduled job calling `ensure_month_partition` for the next 3 months (runs on the job queue above)
  - Partition `pipeline_run` by month and `recording` by tenant (hash) — see "Partition Large Tables" above
  - Archive / detach partitions older than the retention window

## Doable with caveats

- [ ] **Kafka event streaming** (1–2 days)
  - **Status: Partly done.** Redpanda in docker-compose (profile `kafka`), the `EventBus` with Postgres and Kafka implementations chosen by config, topics keyed by lab id. Tested with stand-in producers and consumers. Hosted brokers are supported: `kafka_client_config()` in `events/bus.py` gives the producer and every consumer TLS and SASL (`RADREPORT_EVENTS__KAFKA_SECURITY_PROTOCOL`, `_SASL_MECHANISM`, `_USERNAME`, `_PASSWORD`, `_CA_LOCATION`; `tests/unit/test_kafka_config.py`). Not yet run against a live broker, local or hosted.
  - Redpanda (Kafka-compatible, lighter) in `docker-compose.yml`
  - Outbox relay publishes to topics; consumers: HL7/FHIR export, critical alerts, metering, analytics
  - One `EventBus` interface with Postgres and Kafka implementations, chosen by config
  - **Caveat:** no lasting free hosted Kafka found (Confluent offers trial credits only).
    The hosted demo runs the Postgres implementation; Kafka runs locally and in the crash-test demo.
  - Partition topics by `tenant_id` so one lab's events stay in order

- [x] **Sharding by lab** (3–4 days)
  - **Status: Done.** Consistent-hash ring with 128 virtual nodes and a `lab_shard` pin table, lab rows mirrored to shards, `/ready` checks every shard, admin lab lists fan out. With caveats as planned: two databases on one server, moving a lab is out of scope. `tests/db/test_sharding.py` (run with `RADREPORT_TEST_SHARD_URL`) shows adding a shard moves about 1/N of labs and isolation holds per shard.
  - Consistent-hash ring (virtual nodes) mapping `tenant_id` → shard; explicit override table for pinning a big lab
  - Global database for untenanted tables (`platform_user`, model catalog, shard map); tenant tables on shards
  - `db/session.py` resolves the shard before `bind_tenant`
  - Migrations and RLS policies applied to every shard; `/ready` checks all of them
  - Admin "list all labs" fans out to every shard and merges
  - **Caveat:** demo with 2 databases on one Postgres server; moving a lab between shards is out of scope
  - Test: adding a shard moves only ~1/N of labs; isolation tests pass on each shard

- [ ] **Read replicas** (1 day code + replica setup)
  - **Status: Partly done.** Code done as in "Add Read Replicas" above, including never routing the review screen; no streaming replica has been set up.
  - `read_session()` alongside the write session; route dashboards, cost views and exports only
  - Never route the review screen or anything read right after a write (replication lag breaks read-your-writes)
  - Falls back to the primary when no replica URL is configured
  - **Caveat:** a real replica needs a second Postgres with streaming replication — fine locally, rarely free when hosted
  - Builds on "Add Read Replicas" above

## Configuration, not code

- [ ] **CDN via Cloudflare free plan** (1 hour)
  - **Status: Partly done.** The app side is done: versioned static assets are `public, max-age=31536000, immutable`, everything else `private, no-store`, audio goes through 60-second signed S3 links. Cloudflare setup steps are in ops/cdn/cloudflare.md; the zone itself has to be set up on your Cloudflare account. There is no separate features page to cache.
  - Cache `/ui/static/*` and the features page; set long `Cache-Control` on static assets
  - **Never cache audio.** Serve it through short-lived signed S3 URLs instead of streaming through the app

---

# Keys and accounts to set up (2026-10-07)

Each key has a blank line in `.env` (and is documented in `.env.example`). Fill it in, then
`make restart`. A blank value leaves that feature off; nothing else depends on it. `.env` is
copied into the process environment at start-up, so the unprefixed keys (`BIOPORTAL_API_KEY`,
`GOOGLE_TRANSLATE_API_KEY`, `TEMPLATE_MODEL_API_KEY`, and the provider keys) are read from it too;
a variable already set in the shell wins.

### `RADREPORT_REDIS_URL` — Upstash Redis for the hosted demo (free tier)
1. Sign up at https://console.upstash.com.
2. **Create Database** → Redis, pick the region nearest the app, keep TLS on.
3. On the database page, copy the connection string, `rediss://default:<password>@<host>.upstash.io:6379`.
4. Set `RADREPORT_REDIS_URL` to it. The app logs `shared_cache_backend backend=redis` at start-up.

### `BIOPORTAL_API_KEY` — RadLex term lookup (free)
1. Create an account at https://bioportal.bioontology.org/accounts/new and confirm the email.
2. Open https://bioportal.bioontology.org/account; the **API Key** is shown there.
3. Set `BIOPORTAL_API_KEY`. The onboarding step *Look terms up in RadLex* then queries RadLex.

### `GOOGLE_TRANSLATE_API_KEY` — online translation of unknown Hindi words (pay per use)
1. At https://console.cloud.google.com create a project and attach a billing account.
2. **APIs & Services → Library** → *Cloud Translation API* → **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**. Edit the key: restrict it to
   *Cloud Translation API* and, if the server has a fixed IP, to that IP.
4. Set `GOOGLE_TRANSLATE_API_KEY`, then turn on `languages.online_translation` for the lab in the
   admin panel's System settings. Only single unknown words are sent, never a sentence.

### `TEMPLATE_MODEL_API_KEY` — the Qwen template model server
1. Stand up the server as in docs/TEMPLATE_MODEL.md: Ollama (`ollama pull qwen2.5:7b-instruct`, no
   key) or vLLM on a GPU host (for example RunPod or Lambda), started with `--api-key <random string>`.
2. Put that string in `TEMPLATE_MODEL_API_KEY` (leave blank for Ollama).
3. Admin panel → **Models & providers** → add an OpenAI-compatible provider with the server's
   endpoint and `TEMPLATE_MODEL_API_KEY` as its key variable, then assign it to the lab's
   `template_parse` step.

### `RADREPORT_DB__REPLICA_URL` — a streaming read replica
- Hosted Postgres (for example AWS RDS or Neon): create a read replica in the provider's console and
  use its endpoint.
- Self-managed: on the primary set `wal_level = replica`, create a role with `REPLICATION`, allow it
  in `pg_hba.conf`, then on the replica host run `pg_basebackup -h <primary> -U <role> -D <datadir> -R`
  and start it.
- Either way the URL uses the non-owner role:
  `postgresql+psycopg://radreport_app_login:<password>@<replica-host>:5432/radreport`. Reads fall back
  to the primary while the replica lags more than `RADREPORT_DB__REPLICA_MAX_LAG_SECONDS`.

### `RADREPORT_DB__PGBOUNCER_ADMIN_URL` — the pool dashboard (no key to obtain)
Usually leave blank: with `RADREPORT_DB__PGBOUNCER=true` the dashboard uses the database URL on the
`pgbouncer` database. Set it only when the console is reached differently; the role must be
listed in PgBouncer's `stats_users` (`ops/pgbouncer/pgbouncer.ini.template` already lists the app role).

### Kafka (`RADREPORT_EVENTS__BUS`, `RADREPORT_EVENTS__KAFKA_BOOTSTRAP`)
Local Redpanda needs no key: `docker compose --profile kafka up -d`, then set the bus to `kafka` and
the bootstrap to `localhost:9092`. A hosted broker needs TLS and SASL; set `RADREPORT_EVENTS__KAFKA_SECURITY_PROTOCOL=SASL_SSL`
and the mechanism, username and password (lines are in `.env`):
- **Confluent Cloud:** create a cluster at https://confluent.cloud → **API keys → Add key** (scope it
  to the cluster). Bootstrap is on **Cluster settings** (`pkc-….confluent.cloud:9092`); mechanism
  `PLAIN`; username = API key, password = API secret.
- **Redpanda Cloud:** create a cluster at https://cloud.redpanda.com → **Security → Create user**
  (SCRAM-SHA-256), and give it ACLs on topics `radreport.*` and consumer groups `radreport-*`.
  Bootstrap is on the cluster overview; mechanism `SCRAM-SHA-256`.
- **AWS MSK:** enable SASL/SCRAM on the cluster, store the user in Secrets Manager and associate it;
  mechanism `SCRAM-SHA-512`, bootstrap from **View client information**.
- **Aiven:** create a Kafka service with SASL enabled; download its CA certificate and set
  `RADREPORT_EVENTS__KAFKA_CA_LOCATION` to the file; mechanism `SCRAM-SHA-256`.

Create the topics first (`radreport.` + each topic name in `events/outbox.py`) unless the broker
auto-creates them. Then run the relay (`make relay`) and a consumer (`python -m radreport.events
<name>`); both take the same settings.

### Cloudflare (no app key)
1. Sign up at https://dash.cloudflare.com, **Add a site**, and change the domain's nameservers at the
   registrar to the two Cloudflare gives you.
2. Follow ops/cdn/cloudflare.md for the cache rules.
3. If the public hostname differs from the one the app sees, add it to `RADREPORT_TRUSTED_ORIGINS`.

### UMLS licence — SNOMED CT (nothing reads it yet)
1. Create a UTS account at https://uts.nlm.nih.gov/uts/signup-login and request a UMLS licence;
   approval takes a few working days. SNOMED CT use is free in member countries (India is one).
2. After approval, the API key is on your UTS profile page.
3. No code uses it yet: the local SNOMED CT mapper still has to be built, so there is no `.env`
   line for it.
