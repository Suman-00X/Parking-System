# TODO Roadmap (2026-10-05)

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

- [ ] **Add Query Instrumentation** (4 hours)
  - Enable SQLAlchemy echo in test mode
  - Add slow-query logging (queries > 100ms)
  - Set up Postgres `log_min_duration_statement = 100`
  - Add metrics: query count, query time percentiles
  - Integration: `radreport/db/session.py`

- [ ] **Increase Connection Pool** (1 hour)
  - Change default from `pool_size=10` to `pool_size=30-50`
  - Test under peak load (500 concurrent users)
  - Monitor Postgres memory usage
  - File: `radreport/db/session.py` line 42

- [ ] **Add Request-Scoped Caching** (3 hours)
  - Cache tenant config per request
  - Cache model assignments per request
  - Cache user roles per request
  - Use FastAPI dependency with cache lifetime
  - Location: `radreport/api/deps.py` (new request context manager)

- [ ] **Identify Missing Indexes** (2 hours)
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

- [ ] **Fix Admin Panel Queries** (4 hours)
  - Location: `radreport/api/routes/admin_panel.py`, `radreport/api/routes/admin_api.py`
  - Add `selectinload(Tenant.branding)` on lab list
  - Add `selectinload(Tenant.users)` on lab detail
  - Add `joinedload(PlatformUser.sessions)` on user admin
  - Test: compare query counts before/after

- [ ] **Fix Report Generation Queries** (3 hours)
  - Location: `radreport/pipeline/stages/`
  - Add `joinedload(Recording.study)` in extraction stage
  - Add `joinedload(Recording.metadata)` early
  - Add `selectinload(Study.findings)` for verification stage
  - Profile cost reduction

- [ ] **Fix Onboarding List Queries** (3 hours)
  - Location: `radreport/api/routes/onboarding.py`
  - Add eager loading for ImportBatch relationships
  - Add pagination (LIMIT/OFFSET)
  - Fix: batch status endpoint

- [ ] **Implement Batch Insert Operations** (4 hours)
  - Location: `radreport/onboarding/` (import handlers)
  - Replace per-row inserts with `bulk_insert_mappings()`
  - Batch size: 1000 rows per batch
  - Expected: 10-50x faster for large imports
  - Test with 5000-row onboarding corpus

- [ ] **Implement Batch Updates** (2 hours)
  - Location: `radreport/pipeline/` (pipeline writes)
  - Add batch flush for pending_writes
  - Use `bulk_save_objects()` for stage_execution updates
  - Profile: compare 100-row flush timing

- [ ] **Add Query Result Pagination** (2 hours)
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

- [ ] **Deploy PgBouncer** (4 hours)
  - Set up connection pooler in front of Postgres
  - Config: `pool_mode = transaction`
  - Expected: 10x more app connections, same DB load
  - Test failover scenarios
  - Monitoring: bounce counts, pool utilization

- [ ] **Add Read Replicas** (2-3 days)
  - Create read replica of primary Postgres
  - Add read/write routing in app
  - Routes for read replicas:
    - Admin panel queries
    - Report view endpoints
    - Metering/cost queries
  - File: `radreport/db/session.py` (add read_replica_session())
  - Keep writes on primary (necessary for RLS)

- [ ] **Partition Large Tables** (3-4 days)
  - Partition `pipeline_run` by month (created_at)
  - Partition `recording` by tenant_id
  - Partition `training_corpus_item` by tenant_id
  - Create migration (alembic)
  - Test: query performance, DELETE performance

- [ ] **Enable VACUUM Tuning** (1 hour)
  - Increase autovacuum on high-churn tables
  - Set aggressive for: `pipeline_run`, `stage_execution`, `recording`
  - Monitor bloat with `pg_bloat_check`

- [ ] **Enable Column Compression** (2 hours)
  - Add COMPRESSION=pglz to large text columns
  - Columns: transcript, json_data, audio_metadata
  - Expected: 50-70% storage reduction
  - Slight query slowdown (acceptable tradeoff)

---

## PHASE 4: Advanced (Optional, long-term)

### Priority: MEDIUM

- [ ] **Async Database Driver** (1-2 weeks)
  - Migrate to `sqlalchemy[asyncio]` or `asyncpg`
  - Change: sync Session → async AsyncSession
  - Expected: 20-30% more req/sec capacity
  - Large refactor; schedule separately

- [ ] **LLM Response Caching** (1 week)
  - Cache LLM responses keyed on (stage, input_hash)
  - Backend: Redis or Postgres jsonb
  - Expected: 5-15% cost reduction for repeat submissions
  - Location: `radreport/adapters/llm/base.py`

- [ ] **Materialized Views** (3 hours)
  - Create MATERIALIZED VIEW for `v_canonical_eval_set`
  - Refresh daily at low-traffic time
  - Expected: 5% improvement for eval-heavy workloads

- [ ] **Per-Lab Cost Dashboard** (2-3 hours)
  - Expose `v_tenant_metering_rollup` in admin UI
  - Add cost trend chart
  - Add cost/stage breakdown
  - Add anomaly detection (cost spike alerts)
  - Location: `radreport/api/routes/admin_ui.py`

---

## Metrics to Track

### Before Optimization
- [ ] Document baseline:
  - Query count per request (trace random 100 requests)
  - Average query time (p50, p95, p99)
  - DB CPU usage under peak load
  - Monthly query volume
  - Current cost: $10k/month (assumed)

### After Each Phase
- [ ] Phase 1 complete:
  - Query instrumentation set up
  - Slow query log analyzed
  - Connection pool increased
  - Estimated cost savings: $1-2k/month

- [ ] Phase 2 complete:
  - N+1 queries eliminated
  - Batch operations in place
  - Query count per request reduced by ~50%
  - Estimated additional savings: $2-4k/month

- [ ] Phase 3 complete:
  - PgBouncer deployed
  - Read replicas active
  - Large tables partitioned
  - Throughput increased 3-5x
  - Estimated additional savings: $1-3k/month

---

## Validation Tests

- [ ] Query instrumentation working: check slow query log for entries
- [ ] Connection pool increased: verify `SHOW max_connections` and actual conn count
- [ ] Request caching working: profile request handler, cache hit rate > 80%
- [ ] N+1 queries fixed: query count per request < 10 (was > 50 before)
- [ ] Batch operations fast: 5000-row import < 5 seconds (was ~50s)
- [ ] PgBouncer working: app handles 2x connections with same DB load
- [ ] Read replicas working: 90% read queries hit replica (via query tagging)

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
- [ ] Evaluate Qwen 2.5 7B vs Gemini Nano (cost/accuracy/latency)
- [ ] Implement regex + Qwen fallback for unstructured templates
- [ ] Set up Qwen self-hosting or Gemini Nano integration
- [ ] Test with 3 labs (Lab A, B, C)
- [ ] Add confidence thresholds (< 0.8 → LLM)
- [ ] Measure: accuracy before/after

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
- [ ] Add file upload field for shorthand PDFs on the admin panel's onboarding page
- [ ] Create shorthand extraction regex patterns (5 patterns minimum)
- [ ] Merge shorthand_map into keyterm_set before S4
- [ ] Test with 5 different hospital reference formats
- [ ] Add audit trail for shorthand source → keyterm mapping

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
- [ ] Integrate RadLex API for term lookup
- [ ] Build local SNOMED CT concept mapper
- [ ] Test on known synonym pairs (50+ pairs)
- [ ] Document synonyms used for future updates

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
- [ ] Create potential_lexicon_term table
- [ ] Add background job: aggregate edit_events → find new terms
- [ ] Build a lab-side radiologist approval UI (the approvals stay out of the admin panel)
- [ ] Auto-create lexicon_set version on approval
- [ ] Test workflow with Lab A (1 month of live data)

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
- [ ] Define confidence thresholds per term type
- [ ] A/B test thresholds with radiologist
- [ ] Auto-approve high-confidence matches (log for audit)
- [ ] Track radiologist overrides (improve algorithm)

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
- [ ] Support Hindi term input (Latin script + native script)
- [ ] Add translation layer (Hindi → English)
- [ ] Test with Lab A Hindi-speaking radiologists
- [ ] Support 3–5 languages based on customer demand

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
- [ ] Collect Phase 1 radiologist approval data
- [ ] Fine-tune Qwen 7B on collected templates
- [ ] Test fine-tuned model vs base Qwen
- [ ] Deploy self-hosted fine-tuned Qwen
- [ ] Measure accuracy improvement

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

- [ ] **Move G3_speaker_balance limits to configurable settings** (4 hours)
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
  - **Endpoint:** `GET /health` returns JSON `{status: "healthy", checks: {db: true, redis: true, ...}}`
  - **Purpose:** Allow load balancer to route away from unhealthy instances
  - **Design for scale:**
    - Use dependency injection for instance ID: `INSTANCE_ID` env var or UUID header
    - Health check should NOT require database write (read-only, fast)
    - Return per-check status (e.g., db latency, memory usage)
  - **Integration:** `radreport/api/routes/health.py` (new file)
  - **Before production:** Verify with LB that can consume this endpoint
  - **Future:** Add metrics (response time, error rate) to health check

- [ ] **Add environment variables for adapter gates** (2 hours)
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

- [ ] **Postgres job queue** (1 day)
  - New `job` table; workers claim with `SELECT ... FOR UPDATE SKIP LOCKED`
  - Upload enqueues a `run_pipeline` job instead of running it in the request
  - Visibility timeout + attempt count + dead-letter state for poison jobs
  - Fill the empty `radreport/workers/` package with the worker loop
  - Jobs are tenant-scoped: the worker must `bind_tenant` before touching rows
  - Test: two workers never claim the same job; a killed worker's job is reclaimed

- [ ] **Transactional outbox** (half day)
  - New `outbox_event` table written in the same transaction as the change
  - Events: `recording.ingested`, `draft.ready`, `report.signed`, `autonomy.revoked`
  - A relay publishes unsent rows and marks them sent; consumers must be idempotent
  - Test: crash between commit and publish → event still delivered exactly once per consumer

- [ ] **Redis cache** (1 day)
  - Cache tenant config, model assignments and admin sessions; TTL + explicit invalidation on write
  - In-memory fallback when `RADREPORT_REDIS_URL` is unset, so local dev and tests need no Redis
  - Upstash free tier for the hosted demo
  - Cache keys must include `tenant_id` — a missing one is a cross-lab leak that RLS cannot catch
  - Builds on "Add Request-Scoped Caching" and "LLM Response Caching" above

- [ ] **Bloom filter** (half day)
  - Pure Python, no dependency; sized from expected items + target false-positive rate
  - Uses: duplicate-recording pre-check on `content_hash` before the DB lookup; unknown-term pre-check in lexicon matching
  - A "maybe" still goes to the database — the filter only skips work on a definite "no"
  - Per tenant, rebuilt on startup from the DB
  - Test: no false negatives; measured false-positive rate within target

- [ ] **Extend partitioning** (1 day)
  - **Bug:** 0003 creates partitions 12 months ahead at migration time and nothing
    creates more. After that, rows fall into `<table>_default`, pruning stops, and
    creating that month's partition later fails because the default holds its rows.
  - Add a scheduled job calling `ensure_month_partition` for the next 3 months (runs on the job queue above)
  - Partition `pipeline_run` by month and `recording` by tenant (hash) — see "Partition Large Tables" above
  - Archive / detach partitions older than the retention window

## Doable with caveats

- [ ] **Kafka event streaming** (1–2 days)
  - Redpanda (Kafka-compatible, lighter) in `docker-compose.yml`
  - Outbox relay publishes to topics; consumers: HL7/FHIR export, critical alerts, metering, analytics
  - One `EventBus` interface with Postgres and Kafka implementations, chosen by config
  - **Caveat:** no lasting free hosted Kafka found (Confluent offers trial credits only).
    The hosted demo runs the Postgres implementation; Kafka runs locally and in the crash-test demo.
  - Partition topics by `tenant_id` so one lab's events stay in order

- [ ] **Sharding by lab** (3–4 days)
  - Consistent-hash ring (virtual nodes) mapping `tenant_id` → shard; explicit override table for pinning a big lab
  - Global database for untenanted tables (`platform_user`, model catalog, shard map); tenant tables on shards
  - `db/session.py` resolves the shard before `bind_tenant`
  - Migrations and RLS policies applied to every shard; `/ready` checks all of them
  - Admin "list all labs" fans out to every shard and merges
  - **Caveat:** demo with 2 databases on one Postgres server; moving a lab between shards is out of scope
  - Test: adding a shard moves only ~1/N of labs; isolation tests pass on each shard

- [ ] **Read replicas** (1 day code + replica setup)
  - `read_session()` alongside the write session; route dashboards, cost views and exports only
  - Never route the review screen or anything read right after a write (replication lag breaks read-your-writes)
  - Falls back to the primary when no replica URL is configured
  - **Caveat:** a real replica needs a second Postgres with streaming replication — fine locally, rarely free when hosted
  - Builds on "Add Read Replicas" above

## Configuration, not code

- [ ] **CDN via Cloudflare free plan** (1 hour)
  - Cache `/ui/static/*` and the features page; set long `Cache-Control` on static assets
  - **Never cache audio.** Serve it through short-lived signed S3 URLs instead of streaming through the app
