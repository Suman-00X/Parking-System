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
- `record_consent_event()` — the audit chain behind a withdrawal
- `verify_g6_legal_basis()` — the adaptation gate reads this, not a boolean column

---

## 5. Engines

The only place a vendor is named. Everything above calls an interface; swapping
a provider is configuration, not an engineering project.

**13 files, 1,995 lines.** (2 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 452 | `adapters/asr/rover.py` | ROVER multi-engine voting. NULL is a candidate, which suppresses single-engine insertions. |
| 288 | `adapters/llm/registry.py` | Resolves "which model serves task T for tenant X"; refuses activation without a gold-set eval run |
| 200 | `adapters/llm/anthropic_client.py` | Anthropic client with cache-control blocks and usage accounting |
| 172 | `adapters/llm/prompt.py` | `PromptBundle` — makes a cache-hostile prompt order unexpressible |
| 155 | `adapters/llm/openai_compat.py` | OpenAI-compatible client, for locally hosted models |
| 148 | `adapters/asr/whisper_local.py` | faster-whisper behind the engine interface, plus the deterministic stub |
| 138 | `adapters/llm/base.py` | `LLMClient` protocol, request/response/usage types |
| 133 | `adapters/llm/sampling.py` | k-sample fan-out with cache warm-up (sample 1 completes before 2..k) |
| 127 | `adapters/llm/concurrency.py` | Concurrency limiter and circuit breaker |
| 92 | `adapters/llm/pricing.py` | Corrected Sonnet 5 pricing and cached-call cost accounting |
| 90 | `adapters/asr/base.py` | `ASREngine` protocol, word timings, config hashing for idempotent reruns |

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

**10 files, 2,371 lines.** (1 package `__init__` stub omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 590 | `review/session.py` | Open a draft, record revisions, `active_edit_seconds`, categorised edit events |
| 478 | `review/signing.py` | The four refusals between a draft and a signed record; addenda |
| 280 | `export/hl7.py` | HL7 v2 ORU^R01, MLLP-framed |
| 280 | `review/grading.py` | G0–G4, CSE rate, and the feed into autonomy accrual + CUSUM |
| 262 | `export/fhir.py` | FHIR R4 DiagnosticReport + transaction bundle |
| 272 | `review/queue.py` | Priority → alert → flagged count → oldest, filtered by role |
| 153 | `review/feedback.py` | §9.6's "this draft was useless", actually recorded |
| 135 | `review/rbac.py` | The four roles, genuinely different (an assistant may not sign) |
| 6 | `review/__init__.py` | Package docstring |

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

**16 files, 3,341 lines.** (3 package `__init__` stubs omitted below.)

| Lines | File | Purpose |
|---:|---|---|
| 443 | `autonomy/accrual.py` | Evidence gathering and the Beta-Binomial posterior |
| 443 | `eval/goldset.py` | §5.3-stratified assembly, freeze, permanent training exclusion |
| 423 | `eval/bakeoff.py` | ASR bake-off: per-partition, insertions tracked independently |
| 374 | `monitoring/drift.py` | PSI against an explicit baseline window |
| 360 | `autonomy/grant.py` | Bayesian sequential grant, mechanical CUSUM revocation |
| 348 | `adaptation/gates.py` | §8.6.5's six gates; two unimplemented and failing closed |
| 321 | `eval/harness.py` | Eval runner, scopeable to one `task_key` |
| 175 | `eval/gates.py` | Release-gate evaluation with per-stratum breakdowns |
| 158 | `eval/metrics/asr_metrics.py` | WER, INS_RATE, CTER — all from one alignment |
| 103 | `eval/metrics/routing_metrics.py` | Routing accuracy, codeword compliance, study-code recall |
| 97 | `eval/metrics/alignment.py` | Token alignment shared by the ASR metrics |
| 90 | `eval/metrics/__init__.py` | Metric registry |
| 272 | `autonomy/release.py` | The release gate: what a grant actually changes, and §4.3's coverage |
| 4 | `autonomy/__init__.py` | Package docstring — Beta observes, Phase 6 grants |

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

---

## 8. Surfaces

HTTP and the admin panel. Thin by rule — access checks, permission checks and
serialisation, no business logic.

**19 files, 3,423 lines**, plus the access policy XML. (3 package `__init__`
stubs omitted below.) Line counts taken on 2026-10-06, after the admin panel
and the access policy landed.

| Lines | File | Purpose |
|---:|---|---|
| 731 | `api/routes/admin_panel.py` | Admin panel pages under `/admin`: sign-in, labs, lab page, readiness, onboarding, providers, users |
| 377 | `api/access.py` | Access middleware: policy loading, coverage check, rate limits, caller identification |
| 377 | `api/access_policy.xml` | Every route: realm, allowed roles, rate limit, body cap (XML, not Python) |
| 358 | `api/routes/admin_api.py` | Admin panel JSON under `/admin/api`: labs, steps, onboarding, autonomy, adaptation, platform users |
| 325 | `api/routes/onboarding.py` | 17 routes: lab-side onboarding — consents and clinical approvals |
| 289 | `api/routes/review.py` | 12 routes: queue, draft, revisions, signing, grading, feedback, audio |
| 233 | `admin/modelconfig.py` | Providers, model definitions, per-lab per-step assignment |
| 223 | `api/routes/review_ui.py` | Review screen + queue screen |
| 185 | `admin/auth.py` | scrypt passwords, server-side sessions, revocation |
| 154 | `api/routes/ga.py` | 6 routes: lab-side autonomy read/revoke, release coverage, HL7 + FHIR export, drift |
| 117 | `admin/onboarding_steps.py` | The onboarding uploads and mining steps an admin runs, shared by the pages and the API |
| 99 | `admin/users.py` | Add, deactivate, reactivate and reset product admin and support accounts |
| 93 | `api/deps.py` | The caller the middleware identified, and one-lab session binding |
| 88 | `admin/cli.py` | Create the first admin, reset a password, revoke sessions |
| 87 | `api/app.py` | App factory, router wiring, access middleware, `/health` (liveness) and `/ready` (dependencies) |
| 60 | `api/routes/ingest.py` | Capture-only upload: validate, store, audit |

### `api/app.py` — the FastAPI application
Assembles the routers and puts the access check in front of all of them.

- `create_app()` mounts every router, runs `verify_coverage()` against the
  policy — the app refuses to start on a mismatch — and installs `AccessMiddleware`
- `current_revision()` / `head_revision()` so a schema drift is visible at boot

### `api/access_policy.xml` — who may call what
The single list of every route the app serves. Adding a role or a permission is
an edit here, not in code.

- `<roles>` — `product_admin` and `support` (admin realm); `lab_admin`,
  `radiologist`, `transcriptionist`, `auditor` (lab realm)
- `<rate-limits>` — named sliding-window limits, keyed by caller or by IP
- `<routes realm="public|admin|lab">` — method, path, `<allow role>` list,
  `rate-limit`, `max-body-bytes`, and `environments` for the dev-only docs routes
- A role may only be allowed on a route of its own realm; the parser refuses the file otherwise

### `api/access.py` — the access middleware
Every request passes through it before any handler runs, so a route cannot be
reached without a policy entry and a caller the policy allows.

- `parse_policy()` / `load_policy()` — validate the XML once at startup
- `verify_coverage()` — served routes and policy routes must match exactly
- `AccessMiddleware` — unlisted route `404`; declared body over the cap `413`;
  rate limit `429` with `Retry-After`; caller identification `401` (or a `303` to
  `/admin/login` for a signed-out browser on a panel page); role check `403`
- Admin realm: the `radreport_admin` session cookie. Lab realm: the placeholder
  `X-User-Id` / `X-Tenant-Id` headers, with the user looked up inside that tenant
  and their stored roles checked
- `RateLimiter` — in-process sliding window, so limits are per worker
- `AccessPolicy.allows()` — used by the panel to hide controls a role cannot use

### `api/deps.py` — request dependencies
Holds the boundary invariant: a request is bound to exactly one tenant before it
touches a tenant-scoped table, and a product admin binding to a lab writes an
audit row. Authentication itself happens in `api/access.py`.

- `current_admin()` / `current_principal()` — read the caller the middleware identified
- `get_db()` — a session bound to the lab user's own tenant
- `admin_lab_session()` / `get_admin_lab_db()` — a session bound to the
  `{tenant_id}` in the path, with `select_org()` writing `admin_org_selected`
- `client_ip()` for audit rows

### `api/routes/ingest.py` — capture-only ingest
Upload → validate → store → `recording` row → audit. No pipeline kicked off
here, deliberately.

- `POST` upload returning `IngestResponse`, idempotent on re-upload

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

- `review_screen()` and `queue_screen()`; `render_field()`, `render_retractions()`
- `static_file()` serves the two ES modules with no build step

### `api/static/` — the two client-side pieces
The only things the client genuinely must do.

- `review.js` — focus-time timer and per-field click-to-listen
- `review.css` — the review screen's styles, also used by the admin panel

### `api/routes/admin_panel.py` — the admin panel's pages
Server-rendered, for the same reason as the review screen. Every action is a
form POST that redirects back with a URL-encoded `?error=` or `?notice=`.

- Sign in / out; the login page shows configured demo accounts on a test-credentials tab
- Lab list (offboarded hidden unless `?show=all`) and registration, with
  server-side slug validation and `training_consent_ref` required when pooling
  consent is ticked
- Lab page: status-change form offering only legal targets, per-step model
  proposal and *activate* buttons; readiness page, bound to the lab's session
- Onboarding page: overview, roster and template uploads, a button per mining step, merge proposals
- Providers and models; platform users
- Controls a `support` account cannot use are hidden, by asking the access policy

### `api/routes/admin_api.py` — the admin panel's JSON API
The same operations as the pages, for scripts and tests, under `/admin/api`.
Every lab-scoped route takes the lab from its `{tenant_id}` path segment.

- Labs: list, register, readiness, status change
- Models per step: steps view, propose, activate (gated on a gold-set eval run)
- Onboarding: status, roster, templates, corpus, merge proposals, `steps/{step}`
- Autonomy read, open accrual, grant, revoke; adaptation gates and require-gates
- Platform users: list, create, deactivate, reactivate, reset password

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
- `revoke_all_sessions()` for a compromised account

### `admin/users.py` — platform users
Lets a product admin manage who signs in to the panel without a shell.

- `list_platform_users()`, `create_platform_user()`, `set_active()`, `reset_password()`
- Refuses to deactivate yourself or the last active product admin
- Deactivation and password reset end every session; every change is audited

### `admin/onboarding_steps.py` — onboarding steps an admin runs
One implementation behind both the onboarding page and the admin API.

- `onboarding_overview()` — corpus verification, gold progress, active rules, recent batches, readiness
- `import_roster_file()`, `submit_template_files()`, `load_corpus_records()`, `propose_template_merges()`
- `STEPS` — `derive-map`, `lexicon-mine`, `collision-audit`, `mine-variants`,
  `boilerplate-mine`, `critical-rules-seed`; `run_step()` runs one by name
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

## Placeholders

Five directories exist in the tree and contain no code. They imply capability
that is not there; fill them or delete them.

| Path | Intended for |
|---|---|
| `radreport/workers/` | background job runners — the pipeline currently runs inline |
| `radreport/prompts/` | prompt text as data — prompts are built in `adapters/llm/prompt.py` and the stages |
| `radreport/adapters/dicom/` | DICOM metadata lookup |
| `radreport/adapters/hl7/` | inbound HL7 (orders); outbound lives in `export/hl7.py` |
| `radreport/pipeline/stages/specialists/` | per-modality specialist stages |

---

# Summary tables

Counts from the filesystem on 2026-10-05. Package `__init__.py` stubs of three
lines or fewer are included in the totals but omitted from the per-module tables
above; the stub count is noted under each module heading.

## By module

Grouped by **when the code runs**, which is how the sections above are ordered.

| # | Module | Files | Lines | Share | Runs |
|---|---|---:|---:|---:|---|
| 1 | [Foundation](#1-foundation) | 37 | 6,053 | 19.8% | always |
| 2 | [Onboarding](#2-onboarding) | 12 | 4,808 | 15.8% | once per lab |
| 3 | [Capture](#3-capture) | 5 | 645 | 2.1% | per recording |
| 4 | [Pipeline](#4-pipeline) | 32 | 7,248 | 23.8% | per report |
| 5 | [Engines](#5-engines) | 13 | 1,995 | 6.5% | called by the pipeline |
| 6 | [Review and export](#6-review-and-export) | 10 | 2,371 | 7.8% | per draft, then per signature |
| 7 | [Governance](#7-governance) | 16 | 3,341 | 10.9% | out of band |
| 8 | [Surfaces](#8-surfaces) | 16 | 4,057 | 13.3% | per HTTP request |
| | **Total** | **141** | **30,518** | | |

The two largest are the ones to expect: the Pipeline is sixteen stages, and the
Foundation carries the 58-table schema plus every shared primitive. Capture is
the smallest at 2.1% and does the least on purpose — it validates, stores and
audits, and kicks off nothing.

## By category

The same 141 files cut by **what a file is**, which is the cut that matters when
you are deciding where a change belongs rather than when it runs.

| Category | Files | Lines | Share |
|---|---:|---:|---:|
| Domain logic | 77 | 19,222 | 63.0% |
| DB / schema | 24 | 4,304 | 14.1% |
| Controllers (HTTP) | 12 | 3,084 | 10.1% |
| Adapters (external I/O) | 16 | 2,159 | 7.1% |
| Helpers / shared | 12 | 1,749 | 5.7% |
| **Total** | **141** | **30,518** | |

Adapters are listed apart from domain logic because they are the only code that
talks to Postgres, S3, an LLM or an ASR engine. Counted as logic instead, that is
**93 files and 21,381 lines**.

`DB / schema` splits three ways, and the middle one is not editable code:

| Kind | Files | Lines | Editing rule |
|---|---:|---:|---|
| ORM models | 13 | 3,110 | Declare the 58 tables. Edit freely — but **any change here needs a new migration.** |
| Migrations | 5 | 517 | **Append-only history, not code.** See the table under [Foundation](#1-foundation). |
| Infrastructure | 5 | 592 | The machinery both rely on: `session.py`, `introspect.py`, `base.py`, `bootstrap.py`, `env.py`. |

## Data

**58 tables, 752 columns.**

| Tenancy class | Tables | Meaning |
|---|---:|---|
| Tenant-scoped | 43 | `tenant_id NOT NULL`, RLS policy with `FORCE`, composite FKs to other scoped tables |
| Tenant-NULLable | 12 | NULL = global: model catalog, global lexicon, canonical eval sets, audit log |
| No tenant | 3 | The platform realm: `tenant`, `platform_user`, `admin_session` |

A new table on neither exception list and with no `tenant_id` **fails the
build** — `core/tenancy.py` declares the lists and
`tests/unit/test_schema_tenancy.py` enforces them.

| Area | Tables | Declared in |
|---|---:|---|
| Onboarding | 10 | `db/models/onboarding.py` |
| Knowledge | 8 | `db/models/knowledge.py` |
| Reports | 8 | `db/models/reporting.py` |
| Tenancy | 5 | `db/models/tenancy.py` |
| Identity | 4 | `db/models/identity.py` |
| ASR | 4 | `db/models/asr.py` |
| Review | 4 | `db/models/review.py` |
| Eval | 4 | `db/models/evaluation.py` |
| Model config | 4 | `db/models/modelconfig.py` |
| Orchestration | 3 | `db/models/orchestration.py` |
| Adaptation | 3 | `db/models/adaptation.py` |
| Ingestion | 1 | `db/models/ingestion.py` |

Widest tables, which is where the detail lives: `recording` 27 columns (every
§9.8 quality measurement plus the §10.4 consent-derivation inputs),
`stage_execution` 21 (per-stage cost, tokens, cache hits, resolved model id),
`study` 19, `autonomy_class` 19, `model_adaptation_run` 18,
`template_version` 18.

Three tables are partitioned by month because they grow per-event rather than
per-report: `asr_segment`, `edit_event`, `audit_log`.

## HTTP routes

**93 routes: 89 across 8 files, plus FastAPI's four documentation routes.**
Every one of them lives in [Surfaces](#8-surfaces) — it is the only module that
speaks HTTP — and every one is listed in `api/access_policy.xml`, which
`create_app()` checks at startup. [API.md](API.md#appendix-a--index-by-prefix)
has the roles, rate limit and body cap of each.

| Routes | File | Surface |
|---:|---|---|
| 24 | `api/routes/admin_panel.py` | Admin panel pages and form handlers (**renders HTML**) |
| 24 | `api/routes/admin_api.py` | Admin panel JSON: labs, steps, onboarding, autonomy, adaptation, platform users |
| 17 | `api/routes/onboarding.py` | Lab-side onboarding: consents and clinical approvals |
| 12 | `api/routes/review.py` | Queue, draft, revisions, signing, grading, feedback, audio |
| 6 | `api/routes/ga.py` | Lab-side autonomy read/revoke, release coverage, HL7 + FHIR export, drift |
| 3 | `api/routes/review_ui.py` | Review screen, queue screen, static assets (**renders HTML**) |
| 2 | `api/app.py` | `/health` (liveness) and `/ready` (connection + schema revision) |
| 1 | `api/routes/ingest.py` | Capture-only upload: validate, store, audit |
| 4 | FastAPI | `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` — local, test and development only |

`/health` returning 200 against an unreachable database was a real bug; `/ready`
checks the connection **and** that the schema revision matches the code's head,
and `make run` waits on it.

### By the module behind them

The same 89 routes, attributed to the module whose behaviour each one calls down
into rather than to the file it sits in. This is the view to read when tracing a
request. Most admin operations are served twice — a page and a JSON route — and
both are counted.

| Module | Routes | Prefix | File |
|---|---:|---|---|
| [2 Onboarding](#2-onboarding) — lab registration and lifecycle | 7 | `/admin`, `/admin/api` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [2 Onboarding](#2-onboarding) — uploads, mining steps, overview, readiness | 13 | `/admin/.../onboarding`, `/admin/api/.../onboarding` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [2 Onboarding](#2-onboarding) — lab-side approvals | 17 | `/onboarding` | `routes/onboarding.py` |
| [3 Capture](#3-capture) | 1 | `/ingest` | `routes/ingest.py` |
| [4 Pipeline](#4-pipeline) | **0** | — | **no HTTP trigger exists** |
| [5 Engines](#5-engines) — providers, models, per-step assignment | 8 | `/admin`, `/admin/api` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [6 Review](#6-review-and-export) — API | 12 | `/review` | `routes/review.py` |
| [6 Review](#6-review-and-export) — screens | 3 | `/ui` | `routes/review_ui.py` |
| [6 Export](#6-review-and-export) — HL7 + FHIR | 2 | `/ga/export` | `routes/ga.py` |
| [7 Governance](#7-governance) — autonomy and drift, lab side | 4 | `/ga` | `routes/ga.py` |
| [7 Governance](#7-governance) — autonomy and adaptation, admin side | 6 | `/admin/api/labs/{tenant_id}` | `routes/admin_api.py` |
| [8 Surfaces](#8-surfaces) — sign-in and platform users | 14 | `/admin`, `/admin/api` | `routes/admin_panel.py`, `routes/admin_api.py` |
| [8 Surfaces](#8-surfaces) — ops | 2 | — | `app.py` |

**The pipeline has no route.** Nothing under `api/` imports `pipeline/`, and the
only callers of `build_v1_graph()` and `new_run()` are in
`tests/db/test_pipeline_v1.py`. The largest module here — and the product
itself — is unreachable over HTTP. That is the same fact as the empty
`workers/` directory below: nothing exists to enqueue a run. Trace the pipeline
from that test file, not from a request.

**26 of the 89 render HTML or redirect** rather than return JSON: the admin
panel's pages and form handlers (24, three of them public sign-in routes) and
the two `/ui` screens. They are listed again under [UI](#ui).

### Every route

Grouped by prefix, in source order within each file. Who may call each one is
in [API.md](API.md#appendix-a--index-by-prefix).

#### `/admin` — 24, the admin panel's pages (**HTML**)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/login` | Sign-in page (public) |
| POST | `/admin/login` | scrypt check, server-side session (public, 5 a minute per IP) |
| POST | `/admin/logout` | Revoke the session (public) |
| GET | `/admin` | Redirect to the lab list |
| GET | `/admin/labs` | Lab list and the onboard-a-lab form; `?show=all` includes offboarded labs |
| POST | `/admin/labs` | Register a lab |
| GET | `/admin/labs/{tenant_id}` | One lab: status, all pipeline steps and what serves each |
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
| POST | `/admin/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | Propose near-duplicate merges |
| POST | `/admin/labs/{tenant_id}/onboarding/steps/{step}` | Run one mining or seeding step |
| GET | `/admin/users` | Platform users, with add, deactivate, reactivate and password forms |
| POST | `/admin/users` | Add a product admin or support account |
| POST | `/admin/users/{user_id}/deactivate` | Switch an account off and end its sessions |
| POST | `/admin/users/{user_id}/reactivate` | Switch an account back on |
| POST | `/admin/users/{user_id}/password` | Set a password and end the account's sessions |

#### `/admin/api` — 24, the admin panel's JSON

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
| POST | `/admin/api/labs/{tenant_id}/onboarding/roster` | Import the roster CSV |
| POST | `/admin/api/labs/{tenant_id}/onboarding/templates` | Submit template documents |
| POST | `/admin/api/labs/{tenant_id}/onboarding/corpus` | Load the historical report corpus |
| POST | `/admin/api/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | Propose near-duplicate merges |
| POST | `/admin/api/labs/{tenant_id}/onboarding/steps/{step}` | Run `derive-map`, `lexicon-mine`, `collision-audit`, `mine-variants`, `boilerplate-mine` or `critical-rules-seed` |
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

#### `/ingest` — 1, capture

| Method | Path | Purpose |
|---|---|---|
| POST | `/ingest/recordings` | Upload: §9.8 gates, store, audit. Idempotent by content hash. |

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

#### `/ui` — 3, the review screens (**HTML**)

| Method | Path | Purpose |
|---|---|---|
| GET | `/ui/queue` | Queue screen |
| GET | `/ui/drafts/{draft_id}` | Review screen |
| GET | `/ui/static/{name}` | `review.js` and `review.css` (public) |

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
| GET | `/health` | Liveness only |
| GET | `/ready` | Connection **and** schema revision matches the code's head |

## UI

**1,116 lines, 4 files.** Server-rendered HTML with no build step: no
`package.json`, no bundler, no `node_modules`.

| Lines | File | Purpose |
|---:|---|---|
| 731 | `api/routes/admin_panel.py` | Admin panel: 7 pages, plus 17 form handlers, sign-in routes and the `/admin` redirect |
| 223 | `api/routes/review_ui.py` | Review screen + queue screen |
| 102 | `api/static/review.js` | Focus timer (`active_edit_seconds`), click-to-listen, edit collection |
| 60 | `api/static/review.css` | The entire stylesheet, light and dark; the admin panel uses it too |

The two Python files are also counted under Surfaces above — they are Python
that emits HTML, not a separate tree.

| Route | Screen |
|---|---|
| `GET /admin/login` | Sign in, with a test-credentials tab when demo accounts are configured |
| `GET /admin/labs` | Lab list + onboard-a-lab form |
| `GET /admin/labs/{tenant_id}` | One lab: status change, all pipeline steps, propose and activate a model for each |
| `GET /admin/labs/{tenant_id}/readiness` | The readiness checks, blocking first |
| `GET /admin/labs/{tenant_id}/onboarding` | Onboarding overview, roster and template uploads, step buttons |
| `GET /admin/providers` | Providers and models, with add forms |
| `GET /admin/users` | Platform users, with add, deactivate, reactivate and password forms |
| `GET /ui/queue` | Review queue |
| `GET /ui/drafts/{draft_id}` | Review a draft |

Plus the admin panel's form POST handlers, each of which redirects back to a
page, and the `/ui/static/{name}` asset route.

The browser JavaScript exists for the two things a server cannot do: measure
**focus time** (blur/focus events plus a 20-second idle timeout — §15.2's
commercial argument rests on that number) and **play a cited audio span** from
`provenance_span.audio_start_ms`. Everything else is a form POST and a redirect.

## Tests

**40 files, 9,216 lines, 511 tests.** That is 30% of the repository's lines
against 70% application code.

DB-backed tests skip unless `RADREPORT_TEST_DATABASE_URL` is set, so the unit
suite runs anywhere. They must connect as the **non-owner** `radreport_app_login`
role: a superuser or table owner bypasses RLS, and the isolation tests — the
highest-value tests in the suite — would pass without proving anything.
