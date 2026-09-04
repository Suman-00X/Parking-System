# Modules and submodules

A map of `radreport/` for someone who has to change it. Eight broad modules; the
18 Python packages sit inside them. Each module opens with a file table — line
count, path, one line on what it is for — and then describes each submodule and,
in pointers, the work it actually does.

**141 Python files, 30,518 lines.** Counts taken from the filesystem on
2026-10-05. Aggregate tables are at the bottom: [by module](#by-module),
[by category](#by-category), [data](#data), [HTTP](#http-routes),
[UI](#ui) and [tests](#tests).

Grouping is by **when the code runs** and **what it may depend on**, not by
layer. Dependency direction is one-way: Foundation → everything; Engines → the
Pipeline but never back; Governance reads from Onboarding, Pipeline and Review
but is never called by them; Surfaces call down and are called by nothing.

| # | Module | Packages | Runs | Files | Lines |
|---|---|---|---|---:|---:|
| 1 | [Foundation](#1-foundation) | `core/`, `db/`, `devtools/` | always | 37 | 6,053 |
| 2 | [Onboarding](#2-onboarding) | `onboarding/` | once per lab, before clinical traffic | 12 | 4,808 |
| 3 | [Capture](#3-capture) | `ingest/`, `adapters/storage/` | per recording | 5 | 645 |
| 4 | [Pipeline](#4-pipeline) | `pipeline/`, `knowledge/` | per report | 32 | 7,248 |
| 5 | [Engines](#5-engines) | `adapters/llm/`, `adapters/asr/` | called by the pipeline | 13 | 1,995 |
| 6 | [Review and export](#6-review-and-export) | `review/`, `export/` | per draft, then per signature | 10 | 2,371 |
| 7 | [Governance](#7-governance) | `eval/`, `autonomy/`, `adaptation/`, `monitoring/` | out of band | 16 | 3,341 |
| 8 | [Surfaces](#8-surfaces) | `api/`, `admin/` | per HTTP request | 16 | 4,057 |

---

## 1. Foundation

The substrate. Tenancy, the schema, configuration, and the primitives the other
seven modules are not allowed to re-invent. Depends on nothing in this
repository.

**37 files, 6,053 lines.** (5 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 579 | `core/types.py` | Every enum and value set. The migration builds its CHECK constraints from these. |
| 412 | `db/models/onboarding.py` | §6.10 S0–S7: import batches, artifacts, template candidates, merge proposals, corpus, collisions, readiness checks |
| 378 | `db/models/reporting.py` | §6.6 drafts, field values, provenance spans, verification findings, critical rules and alerts, autonomy observations |
| 372 | `db/models/knowledge.py` | §6.5 templates, versions, fields, lexicon sets/terms/variants, autonomy classes, speaker bias |
| 265 | `core/tenancy.py` | §11.3 exception lists, tenant scope, lifecycle transition gates |
| 256 | `devtools/synthetic.py` | Synthetic audio, dictations, patients, `.docx` templates — no PHI on dev machines |
| 229 | `db/models/modelconfig.py` | §6.14 providers, model definitions, per-(tenant, task) assignments + append-only log |
| 277 | `db/models/review.py` | §6.7 revisions, edit events, final reports, draft-usefulness reports |
| 227 | `db/models/asr.py` | §6.4 ASR runs, segments, transcripts, labelled utterances |
| 206 | `db/models/tenancy.py` | `tenant`, `platform_user`, `admin_session`, training-consent event log, branding |
| 202 | `db/models/evaluation.py` | §6.9 eval sets, items, runs, results |
| 198 | `db/models/adaptation.py` | §6.12 verbatim transcripts, training corpus snapshots, adaptation runs |
| 190 | `db/models/identity.py` | §6.2 app users, radiologist profiles, patients, studies |
| 185 | `db/session.py` | Engine, `tenant_session()`, `system_session()`, `bind_tenant()` — the RLS GUC binding |
| 184 | `db/models/orchestration.py` | §6.8 pipeline runs, stage executions, audit log |
| 152 | `devtools/seed.py` | Seeds the global model catalog, the first product admin, the demo logins and an optional demo lab |
| 166 | `db/introspect.py` | Derives the tenancy facts that migration 0002 and the tests both read |
| 158 | `db/base.py` | Declarative base, `tenant_fk()` composite FKs, tenancy mixins, CHECK builders |
| 150 | `core/errors.py` | Every domain error the system raises deliberately |
| 148 | `db/models/ingestion.py` | §6.3 recordings, with every §9.8 quality measurement |
| 141 | `db/models/__init__.py` | Imports every model so `Base.metadata` is complete for migrations and tests |
| 119 | `core/config.py` | Settings from env and `.env`, `RADREPORT_`-prefixed |
| 91 | `core/text.py` | Sentence splitting that does not cut decimals ("3.2 cm") in half |
| 89 | `db/bootstrap.py` | Creates the least-privileged app and audit roles (non-owner, so RLS is not bypassed) |
| 74 | `db/migrations/env.py` | Alembic environment |
| 61 | `core/hashing.py` | Content hashing — the idempotency backbone |
| 56 | `core/logging.py` | structlog configuration |

### `core/config.py` — settings
Typed settings objects for storage, audio gates, LLM, ASR and observability,
loaded once and cached.

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

- `tenant_session()` / `system_session()` and the engine/sessionmaker singletons
- `bind_tenant()` and `select_org()` for an admin acting on one lab — `select_org()` writes the `admin_org_selected` audit row
- `SET LOCAL` scoping means a pooled connection cannot leak a binding

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

### `db/models/` — the §6 schema (13 files, 57 tables)
One file per §6 area; importing the package registers every table on
`Base.metadata`. A model file not imported here is invisible to both Alembic and
the RLS coverage test.

- `tenancy.py` — tenant lifecycle, platform users, admin sessions, consent log
- `identity.py` — app users, radiologist profiles, patients, studies
- `ingestion.py` — `recording`: one audio file, one report
- `asr.py` — ASR runs, segments, transcripts, utterances
- `knowledge.py` — lexicon sets/terms/variants, templates, versions, fields, speaker bias
- `onboarding.py` — import batches, artifacts, candidates, merge proposals, audit findings
- `reporting.py` — routing decisions, drafts, field values, provenance, verification, alerts
- `review.py` — revisions, edit events, final reports, usefulness reports
- `orchestration.py` — pipeline runs, stage executions, audit log
- `evaluation.py` — eval sets/items/runs/results, with the canonical vs. per-lab split
- `modelconfig.py` — providers, model definitions, per-tenant task assignments and their log
- `adaptation.py` — verbatim transcripts, training-corpus snapshots, adaptation runs

### ⚠️ `db/migrations/versions/` — five revisions, 517 lines
**Append-only. Never edit an applied migration — add a new one.** Editing one
changes what a deployed database is assumed to contain, which the schema itself
cannot detect. `0001` is generated from `Base.metadata`; every revision after it
is a hand-written diff.

`0002` and `0005` need the database **owner** connection (`make migrate-owner`),
because they create roles and grant on new tables. Run as the app role they fail
with a bare permissions error that does not say so.

| Rev | Lines | File | What it does |
|---|---:|---|---|
| 0001 | 45 | `0001_initial_schema.py` | The full §6 model, with `tenant_id` and composite FKs from day one. Short because it builds from the models rather than restating them. |
| 0002 | 177 | `0002_rls_and_roles.py` | `FORCE ROW LEVEL SECURITY`, the app/audit role split, the three enumerated cross-tenant views. **Owner only.** |
| 0003 | 86 | `0003_partitions.py` | Monthly partitions for `asr_segment`, `edit_event`, `audit_log`, plus a default partition |
| 0004 | 64 | `0004_template_version_spoken_code.py` | Scopes spoken-study-code uniqueness to `is_current`. The plain `UNIQUE` made a second template version impossible. |
| 0005 | 145 | `0005_admin_auth_and_model_config.py` | Admin passwords + `admin_session`, `api_key_env_var`, the two ASR task keys. **Owner only.** |

`db/migrations/env.py` (74 lines) is the Alembic environment and is ordinary
code, not history — it is listed in the Foundation table above.

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

---

## 2. Onboarding

The S0–S7 onboarding stages: everything that turns a signed contract into a lab the
pipeline can serve. Runs once per lab and must finish before `tenant.status` can
move `onboarding → pilot`.

**12 files, 4,808 lines.** (1 package `__init__` stub omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 807 | `onboarding/templates.py` | S1 — candidates, merge proposals, promotion under R18 |
| 579 | `onboarding/lexicon.py` | S3 — term mining and the blocking collision audit |
| 523 | `onboarding/corpus.py` | S2 — corpus load, derived template map, usage histogram, referrer prior |
| 494 | `onboarding/critical_rules.py` | S6 — seed candidates, author, approve critical-findings rules |
| 476 | `onboarding/paired_audio.py` | S4 — verbatim queue, corpus hours, surface-variant mining |
| 417 | `onboarding/boilerplate.py` | S5 — rank normals by corpus share, CSV export |
| 348 | `onboarding/readiness.py` | S7 — the seven checks gating onboarding → pilot |
| 334 | `onboarding/roster.py` | S0 — roster import, voice enrollment, the two separate consents |
| 279 | `onboarding/registration.py` | Lab registration and the tenant lifecycle |
| 276 | `onboarding/template_parse.py` | S1's `.docx` parser, stdlib only. PDF refused, not half-parsed. |
| 275 | `onboarding/batches.py` | The import-batch lifecycle every stage shares |

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
- `evaluate_readiness()` returns the report the admin panel's readiness page and its JSON API both render

---

## 3. Capture

The capture-only path. Ships before anything that interprets audio, so the
`current` gold partition accumulates while the rest is being built — and must
keep working when every other module is down.

**5 files, 645 lines.** (2 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 299 | `ingest/audio_gates.py` | §9.8 quality gates: format, SNR, silence, duration |
| 182 | `ingest/service.py` | Capture-only ingest, idempotent by content hash |
| 164 | `adapters/storage/object_store.py` | S3-compatible store (SSE-KMS) + in-memory double |

### `ingest/audio_gates.py` — §9.8 quality gates
Two kinds of failure, kept apart deliberately. A reject is permanent data loss;
a warning is a recording worth keeping with a caveat attached.

- **Reject**: lossy codec, unsupported container — lossy audio can never train ASR later
- **Warn**: low sample rate, poor SNR, mostly silence, odd duration
- `sniff_container()`, `probe_audio()`, `analyse_frames()`, `estimate_snr_db()`, `estimate_silence_ratio()`

### `ingest/service.py` — capture-only ingest
Record → validate → FLAC → object store → `recording` row → audit. No pipeline,
no ASR, no UI, on purpose.

- `ingest_recording()` — idempotent by `content_hash`, so a retried upload is a no-op
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

**32 files, 7,248 lines.** (3 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 450 | `pipeline/stages/routing.py` | Stage 9 — the §8.3.4 cascade. Declines rather than picking the nearest of twenty. |
| 376 | `pipeline/stages/normalise.py` | Stage 3 — phonetic resolution with the margin guard (stops LMC resolving to LMP) |
| 368 | `pipeline/stages/extract.py` | Stage 10 — per-section extraction, k=3, provenance mandatory |
| 348 | `pipeline/stages/verify/rules.py` | Stage 13 — the deterministic §8.3.8 contradiction checks |
| 358 | `pipeline/stages/persist.py` | Stage 16 — the seam to the review surface; emits every domain row |
| 214 | `pipeline/stages/release.py` | Stage 17 — files the report a granted class released (GA, off by default) |
| 331 | `pipeline/stages/critic.py` | Stage 13b — LLM critic + round-trip entailment (Beta) |
| 329 | `pipeline/stages/reconcile.py` | Stage 2b — multi-engine fan-out, ROVER, bounded arbitration (Beta) |
| 291 | `pipeline/stages/repairs.py` | Stage 6 — self-corrections. Splits the utterance; never deletes. |
| 288 | `knowledge/phonetics.py` | Double metaphone, the E-set, the collision audit |
| 285 | `pipeline/stages/study_code.py` | Stage 4 — bounded study-code search; `CODEWORD_COMPLIANCE` vs `STUDYCODE_RECALL` |
| 284 | `pipeline/stages/grounding.py` | Stage 11 — verbatim quote check, outside the LLM (invariant I1) |
| 282 | `pipeline/stages/segment.py` | Stage 5 — segment and classify utterances. Labels only; nothing deleted. |
| 281 | `pipeline/stages/critical.py` | Stage 7 — critical findings on the transcript; writes the alert row |
| 251 | `pipeline/stages/preprocess.py` | Stage 1 — VAD, SNR, peak normalisation |
| 247 | `pipeline/graph.py` | The orchestrator: `stage_execution` rows, commit discipline, shadow mode |
| 321 | `pipeline/state.py` | `PipelineState` — the object every stage reads |
| 275 | `pipeline/stages/providers.py` | Per-tenant knowledge snapshot, injected (stages get no DB session) |
| 240 | `pipeline/stages/post_correction.py` | Stage 2c — the only stage allowed to rewrite the transcript |
| 223 | `knowledge/consent.py` | Derived training eligibility; `G6_legal_basis` |
| 220 | `pipeline/stages/asr.py` | Stage 2 — single-engine ASR with keyterm biasing |
| 238 | `pipeline/v1.py` | The 16-stage graph, plus release at GA. The only place edges are defined. |
| 201 | `pipeline/stages/compose.py` | Stage 12 — deterministic render from `render_spec`, grounded atoms only |
| 192 | `pipeline/stages/sketch.py` | Stage 8 — template-free finding sketch, before routing |
| 277 | `pipeline/stages/route_human.py` | Stage 15 — who reviews it, the §5.4.1 grading sample, and whether anybody reviews it |
| 175 | `pipeline/stages/confidence.py` | Stage 14 — `min(critical) × mean(all)` |
| 139 | `pipeline/timing.py` | Character offset → audio time, for click-to-listen and the training corpus |
| 107 | `pipeline/context.py` | `RunContext`: cost accounting, budget cap, model resolution |
| 101 | `pipeline/contracts.py` | `Stage` / `StageResult` — no stage writes domain tables |
| 9 | `pipeline/stages/verify/__init__.py` | Re-exports the verification stage |

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
