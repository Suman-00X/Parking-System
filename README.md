# radreport

Radiology voice-to-structured-report pipeline. Built from `PLAN.md`, which is
derived from `radiology-reporting-system-design.pdf` (Technical Design Document
v1.0). Where the two disagree, the design doc wins on clinical and safety
questions; the plan wins on sequencing.

**Status: Phases 0–6 built.** A recording goes in, the pipeline produces a
draft, a reviewer signs it, and it exports as HL7 v2 ORU^R01 or FHIR R4. Beta
adds ROVER reconciliation, phonetic post-correction and the LLM critic; GA adds
the autonomy grant/revocation engine, §8.6.5's adaptation gates and drift
monitoring.

Two things are deliberately **not** finished, and both are visible rather than
papered over: Phase 2's bake-off is blocked on audio from the new microphones,
and two of §8.6.5's six adaptation gates are unimplemented and **fail closed**
because their text is not in `PLAN.md` and was not recoverable from the design
PDF.

A lab can be registered, its roster imported with consent captured, its
templates parsed and approved into an immutable version history, its report
corpus loaded and mapped, its lexicon mined and collision-audited, its verbatim
queue worked, its boilerplate ranked and its critical-findings rules authored —
and S7 will tell it what is still missing.

The pipeline runs end to end. Eight of its fifteen stages are deterministic —
the margin guard, self-correction handling, critical-findings alerting,
grounding, the verification rules and the confidence formula — and those are
deliberately the ones carrying the safety properties: pure, replayable and
bit-reproducible, testable without a model. The other seven call a model or an
ASR engine, and each degrades explicitly rather than silently when none is
bound.

Two engine-shaped holes remain, both waiting on the same thing: no ASR engine
has been chosen, because that decision belongs to the Phase 2 bake-off and the
bake-off needs audio from the new microphones.

The review surface is server-rendered rather than a single-page app. A
JavaScript build chain is a dependency in the path of every clinical review,
for a screen that is a form with a timer on it; the two things the client
genuinely must do — measure focus time and play a cited audio span — are two
small ES modules with no build step.

---

## Why Phase 0 looks like this

Three decisions drive everything in this codebase, and all three are cheap now
and a rewrite later.

**Tenancy is enforced, not annotated.** Every table carries `tenant_id` bar a
short exception list; every tenant-scoped table has an RLS policy with `FORCE`;
every foreign key between two tenant-scoped tables is composite. The last one
is the detail §11.4 says decides whether isolation actually holds: with
`tenant_id` everywhere you can still have `report_draft.tenant_id = A`
referencing `recording.tenant_id = B`, and both rows pass their own policy.

**The capture-only ingest path ships first.** Verbatim annotation of the 150
`current` gold items is the project's critical path — the ASR bake-off waits on
it, every release gate waits on the bake-off. Recordings need to start
accumulating before anything can interpret them.

**Prompts are ordered `stable → cache breakpoint → volatile` from the first one
written.** Retrofitting means restructuring every prompt and re-running the
gate. `PromptBundle` makes the wrong order unexpressible rather than merely
discouraged.

## Corrections to the source document

These are implemented, not just noted (see `PLAN.md` §0 for the full working):

| | Design doc | Here |
|---|---|---|
| Sonnet 5 pricing | $3.00 / $15.00 per MTok (§7.9.2) | **$2.00 / $10.00** — that was Sonnet 4.6's rate |
| Model identifiers | `claude-sonnet-5-20260415` (§6.14) | **`claude-sonnet-5`** — the API rejects date suffixes |
| Thinking budget | — | `thinking: {type: "adaptive"}`; `budget_tokens` is rejected |
| `admin` role | `app_user.roles` includes `admin` (§6.2) | **`lab_admin`** — "admin" is reserved for the platform realm |
| `tenant.status` | free text (§6.13) | an enumerated lifecycle, with `onboarding → pilot` gated on S7 |
| `is_training_corpus_eligible` | a settable boolean (§6.3) | **derived** from four independent conditions (§10.4) |
| `template_version` spoken code | `UNIQUE (tenant_id, spoken_study_code)` (§6.5) | **unique among `is_current` rows only** — the plain constraint made a second version of any template impossible, taking §6.5's version history and §9.10's rollback with it (migration 0004) |

## Operations

Every command is a `make` target. `make help` lists them with one-line
descriptions; this section is the order to run them in and the two things that
bite people.

### First-time setup

```bash
make install                         # venv + dependencies (editable install)
cp .env.example .env                 # then edit it — see Configuration below
make up                              # Postgres 16 + pgvector, MinIO (docker)
make migrate-owner                   # create the schema  <- owner, not app role
make seed                            # model catalog + the first product admin (see Administration)

# The app must NOT connect as a superuser or the table owner: both bypass RLS,
# which would make the isolation tests pass vacuously. This creates the
# least-privileged role the app and the tests use.
.venv/bin/python -m radreport.db.bootstrap --app-password <pw>

make admin EMAIL=you@example.com     # or create the first product admin interactively
make check                           # lint + the full test suite
make run                             # start the API in the background
```

Then open **http://127.0.0.1:8000/admin/login**.

Without Docker, a local Postgres 16 works. It needs the `vector`, `pgcrypto`
and `citext` extensions, and `pgvector` may have to be built from source —
Homebrew's bottle ships `.dylib`s for postgresql@17 and @18 only.

### Running the server

| Command | What it does |
|---|---|
| `make dev` | Foreground, auto-reload on edit. Ctrl-C to stop. What you want while writing code. |
| `make run` | Background, 2 workers, waits until `/ready` answers before reporting success. Writes `.uvicorn.pid` and `.uvicorn.log`. |
| `make restart` | `stop` then `run`. Use after changing code or `.env` — neither is picked up by a running process. |
| `make stop` | Kills the background server and removes the pid file. |
| `make status` | Whether it is running, plus `/health` and `/ready`. |
| `make logs` | `tail -f` on `.uvicorn.log`. |

Override host, port or worker count per invocation:

```bash
make run PORT=9000 HOST=0.0.0.0 WORKERS=4
make dev PORT=8001
```

**`/health` and `/ready` are not the same check.** `/health` is liveness — the
process is up, and it checks nothing else on purpose, because a liveness probe
that fails during a brief database blip gets the container killed, which does
not reconnect the database and does lose every in-flight request. `/ready` is
readiness: it opens a database connection and compares the schema revision
against the code's head, returning 503 if either is wrong. Point your deploy
check and your load balancer at `/ready`.

```bash
curl -s localhost:8000/ready | jq
# {"status": "ready", "checks": {"database": "ok", "migrations": "at 0005"}}
```

### Migrations

```bash
make migrate-owner                   # apply as the database owner
make migrate                         # apply as the app role
make revision M="what changed"       # autogenerate a new migration
```

**Use `migrate-owner`.** Migration 0002 creates database roles and reassigns
view ownership, and 0005 grants on a new table — neither of which the app role
may do. `make migrate` exists because it is occasionally what you want, and it
will fail on those two with a permissions error that does not say why. The
owner URL defaults to `$(whoami)@localhost/radreport`; override it with
`OWNER_URL=...`.

To check where a database actually is:

```bash
RADREPORT_DATABASE_URL=<url> .venv/bin/alembic current
```

### Tests

```bash
make test                            # everything
make test-unit                       # only the tests needing no database
make check                           # lint + test, what CI runs
.venv/bin/pytest tests/db/test_review.py -q          # one file
.venv/bin/pytest -q -k "rover or cusum"              # by name
```

DB-backed tests **skip** unless `RADREPORT_TEST_DATABASE_URL` is set, so the
unit suite runs anywhere. They must connect as `radreport_app_login` rather
than the owner: a superuser or owner bypasses RLS, and the isolation tests —
the highest-value tests in the suite — would pass without proving anything.

```bash
export RADREPORT_TEST_DATABASE_URL=postgresql+psycopg://radreport_app_login:<pw>@localhost:5432/radreport_test
```

### Administration

```bash
RADREPORT_SEED_ADMIN_PASSWORD=... make seed                        # first product admin, from code
make admin EMAIL=you@example.com                                   # ...or interactively
make admin-password EMAIL=you@example.com                          # reset a password
.venv/bin/python -m radreport.admin.cli revoke-sessions --email ... # sign out everywhere
```

The admin panel cannot create the first account, because signing in needs an
account and creating one needs a session, so that loop is broken from code by
someone who already has the database: `make seed` creates
`admin@radreport.local` (or `--admin-email`) and sets its password from
`RADREPORT_SEED_ADMIN_PASSWORD`; `make admin` prompts instead, so the password
never lands in the shell history. Every later product admin or support account
is added from the admin panel's **Users** page.

**The admin panel lives at `/admin`** (pages) and `/admin/api` (the same
operations as JSON). Both use the session cookie from `/admin/login`; there is
no header that stands in for it. A `support` account can see everything and
change nothing. From it a product admin can:

- **onboard a lab** — including the §10.8 pooling clause, captured at
  registration because that is the one moment the answer is known;
- **register providers and models** — a cloud provider names the *environment
  variable* holding its key, never the key; a locally hosted model is
  configured by its address;
- **assign a model to each pipeline step, per lab** — all ten steps including
  `asr_primary`, with §7.9.4 enforced (a consequential task refuses a local
  model) and §6.14's gate intact (an assignment is *proposed*; nothing serves
  clinical traffic without a gold-set `eval_run`);
- **move a lab through its lifecycle** — onboarding → pilot is refused until
  every readiness check passes;
- **run a lab's onboarding** — upload its roster, templates and report corpus
  and run the mining steps. The clinical approvals (template candidates,
  merges, collision findings, critical rules) stay with the lab's radiologists
  on the lab-side `/onboarding` routes;
- **manage platform users** — add, deactivate or reactivate product admins and
  support accounts, and reset their passwords.

### Who may call what

Every route is listed in **`radreport/api/access_policy.xml`** with the roles
allowed to call it, its rate limit and its request-size cap. A middleware checks
each request against that file before any handler runs: an unlisted route is
refused, a wrong role gets 403, too many requests get 429. The app refuses to
start if the file and the routes disagree. To give `support` a new permission,
add an `<allow role="support"/>` to that route. Rate limits are counted in each
worker process's memory, so with `WORKERS=N` the effective limit is up to N
times the configured one.

### Configuration

Settings are read from the environment and `.env`, prefixed `RADREPORT_`, with
`__` for nesting (`RADREPORT_STORAGE__BUCKET`). The ones that matter:

| Variable | Why |
|---|---|
| `RADREPORT_DATABASE_URL` | The app's connection. Must be the **non-owner** role. |
| `RADREPORT_TEST_DATABASE_URL` | Unset ⇒ DB-backed tests skip. |
| `RADREPORT_ENVIRONMENT` | `local` / `test` / `development` serve the API docs (`/docs`, `/openapi.json`) and send the admin cookie without `Secure`. Anything else hides the docs and requires HTTPS for the cookie. |
| `RADREPORT_STORAGE__*` | S3-compatible audio store; SSE-KMS in a real deployment. |
| `RADREPORT_SEED_ADMIN_PASSWORD` | Read only by `make seed`, to set the first product admin's password. |
| `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, … | **Unprefixed, and not settings.** Each provider row names the variable it reads, so a second account is a second variable plus a second provider in the admin panel — no code change. |

### Housekeeping

```bash
make fmt        # apply formatting
make lint       # check it
make clean      # caches, pid file, server log
make down       # stop Postgres and MinIO
```

## Layout

Two companion files go deeper: [`MODULES.md`](MODULES.md) maps `radreport/`
for someone who has to change it, and [`API.md`](API.md) is the HTTP contract
— authentication, tenant scoping, every endpoint, and the gaps.

Follows §8.1, with additions where Phases 0–2 needed them.

```
radreport/
  core/          config, types, errors, tenancy, hashing, logging, text
  db/
    models/      the full §6 schema (57 tables)
    migrations/  0001 schema · 0002 RLS + roles + views · 0003 partitions
                 0004 spoken-code uniqueness scoped to the current version
    introspect.py  derives the tenancy facts the migration and tests both use
  autonomy/      §8.3.10 accrual · grant (Bayesian) · revoke (CUSUM)
  adaptation/    §8.6.5's six gates — two unimplemented, failing closed
  export/        HL7 v2 ORU^R01 · FHIR R4 DiagnosticReport
  monitoring/    drift.py — PSI against an explicit baseline window
  pipeline/      Stage/StageResult contracts, PipelineState, orchestrator
    v1.py        the graph: the only place edges are defined (V1 and Beta)
    stages/      preprocess  1 · asr       2 · normalise 3 · study_code 4
                 segment     5 · repairs   6 · critical  7 · sketch     8
                 routing     9 · extract  10 · grounding 11 · compose  12
                 verify     13 · confidence 14 · route_human 15
                 Beta: reconcile (ROVER) · post_correction · critic
                 providers.py  per-tenant knowledge, injected not queried
  adapters/
    llm/         prompt caching, k-sample fan-out, registry, pricing, clients
    asr/         engine interface + Whisper/stub + rover.py (voting)
    storage/     S3-compatible object store
  ingest/        §9.8 quality gates, capture-only service
  knowledge/     phonetics + collision audit, consent derivation
  eval/          harness, §5.2 metrics, release gates
                 goldset.py  §5.3-stratified assembly, freeze, R21 exclusion
                 bakeoff.py  ASR bake-off: per-partition, insertions independent
  onboarding/    the S0–S7 onboarding stages
                 batches.py       batch lifecycle: the spine all stages share
                 roster.py        S0 · templates.py + template_parse.py  S1
                 corpus.py        S2 · lexicon.py  S3 · paired_audio.py  S4
                 boilerplate.py   S5 · critical_rules.py  S6
                 readiness.py     S7 · registration.py  lab lifecycle
  review/        the §7.2 surface: rbac · queue · session · signing
                 grading (G0–G4) · feedback ("this draft was useless")
  admin/         admin panel logic: auth (scrypt + sessions) · users ·
                 modelconfig (providers, models, per-step assignment) ·
                 onboarding_steps (shared by the panel and its API) · cli
  api/           FastAPI: access.py + access_policy.xml (who may call what),
                 admin_panel + admin_api, ingest, onboarding, review, ga
    static/      review.js (focus timer, click-to-listen) · review.css
  devtools/      synthetic data (no PHI on developer machines), seed
```

## The things most likely to be broken by a careless change

Each has a test that fails loudly.

- **`core/tenancy.py`** — `UNTENANTED_TABLES` / `NULLABLE_TENANT_TABLES` are the
  §11.3 exception list. A new table that is on neither list and has no
  `tenant_id` fails the build. Migration 0002 generates its policies from the
  same introspection, so a table cannot be classified and then left unpoliced.
- **`db/base.py:tenant_fk`** — use it, never a bare `ForeignKey`, between two
  tenant-scoped tables. (Where either side has a *nullable* `tenant_id`, a plain
  FK is correct: a composite one is skipped under MATCH SIMPLE when a column is
  NULL, so it would enforce nothing on exactly the global rows.)
- **`adapters/llm/prompt.py`** — unpinned exemplars in the stable region are
  rejected. Retrieved-per-report exemplars invalidate the prefix on every call
  while still *looking* cached.
- **`adapters/llm/sampling.py`** — sample 1 completes before 2..k fire. The
  obvious `asyncio.gather` over all k forfeits ~29% of the LLM bill silently.
- **`adapters/llm/registry.py`** — no activation without a gold-set `eval_run`.
  §6.14 flags this for code review because Postgres `CHECK` cannot express it.
- **`eval/metrics/`** — `WER` and `INS_RATE` stay separate metrics; so do
  `CODEWORD_COMPLIANCE` and `STUDYCODE_RECALL`.
- **`eval/bakeoff.py:rank`** — an engine whose insertions dominate its errors
  sinks regardless of headline WER, and `recommend()` reads the `current`
  partition only. Blending the two numbers selects the engine that invents
  findings (§7.6); reading `legacy` selects the engine that was good on the
  microphones you are replacing (§5.3, R14).
- **`onboarding/templates.py:apply_templates`** — the collision audit runs
  before any row is written, so a batch that would introduce an LMC/LMP pair is
  refused whole rather than applied and then flagged.
- **`onboarding/boilerplate.py:promote_candidate`** — storing a normal
  statement and deciding to emit it are two decisions. `enable_auto_fill`
  defaults to False and a critical field refuses it outright (R4, §6.5).
- **`pipeline/stages/normalise.py`** — the margin guard escalates instead of
  picking when the top two candidates sit within `TAU_MARGIN`. It also does
  **not** rewrite the transcript: char offsets are what provenance cites and
  what grounding verifies verbatim, so a substitution re-bases every quote.
- **`pipeline/stages/repairs.py`** — the retraction applies *backwards* and the
  correction *forwards*. Reversing that turns "left — sorry, right" into a G4
  laterality error. The cue list stays narrow: a cue that fires on ordinary
  speech silently deletes findings.
- **`pipeline/stages/critical.py`** — negation is sentence-local *and*
  positional. A whole-sentence membership test suppresses "no fracture; large
  pneumothorax", and a document-wide one suppresses far more.
- **`pipeline/stages/grounding.py`** — the quote is checked against the cited
  **character range**, not searched for in the transcript. A quote that appears
  somewhere proves nothing about the span the model pointed at.
- **`pipeline/stages/confidence.py`** — `min(critical) × mean(all)`. Averaging
  hides the one weak critical field among thirty strong ones, which is the only
  case the number exists to catch.
- **`core/text.py:split_sentences`** — a period between two digits is not a
  sentence boundary. The naive `[.;\n]+` this replaced cut "no 3.2 cm
  pneumothorax" in half and fired a false critical alert, because the negation
  and the finding landed in different fragments.
- **`pipeline/stages/repairs.py:apply_repairs`** — a repair inside one
  utterance **splits** it. Excluding the utterance whole takes the correction
  out with the retraction, and the finding disappears with no flag.
- **`pipeline/stages/extract.py:merge_samples`** — a value with no citation is
  dropped, not flagged (I1); the k-sample denominator is `k`, not the number of
  samples that answered.
- **`review/session.py:record_revision`** — `active_edit_seconds` is focus
  time, measured by the browser and **clamped to the wall clock** by the
  server. §15.2's commercial argument rests on this number against an 18–36
  second break-even bar; an unbounded client value is one instrumentation bug
  away from becoming the headline metric.
- **`review/rbac.py`** — an assistant revises and a radiologist signs. Two-layer
  supervision is the whole safety model of the assistant path, and a system
  that let an assistant sign would have the same screen and none of it.
- **`review/signing.py:preflight`** — every gate `sign_report` enforces,
  computed without signing, so the screen can disable the button *and say why*.
  The two must not drift: a button that looks available while the call refuses
  teaches people to click and see.
- **`adapters/asr/rover.py`** — NULL is a voting candidate. That is the whole
  anti-hallucination property: a word one engine invented and two did not hear
  loses 2–1 and never reaches the transcript. `AGREEMENT_WEIGHT` leans on
  agreement over confidence, because confidence is self-reported and §7.6's
  failure mode is an engine that inserts confidently.
- **`pipeline/stages/post_correction.py`** — the **only** stage that rewrites
  the transcript, and only because it runs before any offset is recorded. It
  raises if utterances or resolutions already exist. Its position in the graph
  is the safety argument, not the implementation.
- **`autonomy/accrual.py:_beta_cdf`** — checked against closed forms for
  Beta(1,1), Beta(2,2) and the arcsine law. An earlier continued fraction was
  wrong by exactly 1 and returned plausible-looking numbers; a posterior that
  feeds a grant decision is not somewhere to trust code by inspection.
- **`autonomy/grant.py`** — granting is deliberate and hard, revocation is
  mechanical and easy. That asymmetry is the safety argument, and anything that
  makes revocation as hard as a grant inverts it. `DEFAULT_CUSUM_THRESHOLD` was
  chosen from simulated run lengths, and `test_grant.py` re-derives them.
- **`adaptation/gates.py`** — G2 and G5 are unimplemented and fail closed. A
  gate with invented criteria that passes is indistinguishable from one that
  was genuinely satisfied, which defeats the point of a checklist.
- **`export/hl7.py:escape`** — an unescaped `|` truncates the segment, which
  presents as a report that silently loses its second half. The escape
  character is replaced first, or the others get double-escaped.
- **`pipeline/stages/persist.py`** — the seam between the pipeline and the
  review surface. Emits every domain row through `pending_writes` so a shadow
  run discards them by the same mechanism as any other write, and orders the
  utterance inserts so a self-correction's target exists before the row that
  references it.
- **`pipeline/timing.py`** — character offset to audio time. Every provenance
  span's `audio_start_ms` depends on it; without it §7.2's click-to-listen
  plays from 0 ms and §6.7's training rows carry no usable span. `(0, 0, True)`
  means *unknown*, and the flag is what distinguishes it from a real span at
  the start of the recording.
- **`review/grading.py:_feed_autonomy`** — grading is the only place a graded
  report exists, so it is the only place accrual and the CUSUM can be fed. An
  unfed safety monitor is worse than none: it reports as coverage.
- **`api/access_policy.xml` + `api/access.py`** — the only place a route's
  roles are decided. A new route that is not added to the XML makes the app
  refuse to start; a role added to an `<allow>` takes effect everywhere at once.
  Admin identity comes only from the session cookie: the old
  `X-Platform-User-Id` header made the admin realm spoofable and is gone.
- **`api/deps.py:admin_lab_session`** — an admin acting on a lab must go
  through it (or `AdminLabDb`), which binds the session to that lab. Without
  the binding RLS hides every row, and the readiness page reported every check
  as failed for exactly that reason.
- **`admin/modelconfig.py`** — a provider row stores the *name* of the
  environment variable holding its API key, never the key. Every tenant-scoped
  read and write binds the tenant first: an unbound session is refused on write
  and returns **nothing** on read, and the read failure looks like "this lab
  has no configuration" rather than like an error.
- **`api/app.py` `/health` vs `/ready`** — liveness checks nothing but the
  process, because a liveness probe that fails on a database blip gets the
  container killed for no benefit. Readiness opens a connection *and* compares
  the schema revision against the code's head. `/health` alone returns 200
  against a misconfigured database URL, which is how a server reports healthy
  and fails every request that touches a table.
- **`pipeline/stages/persist.py`** — the `seq -> utterance row` map is keyed on
  each row's own `seq`. The rows are emitted in *reference* order so a
  self-correction's target is inserted before the row pointing at it, which is
  not sequence order — zipping two lists paired every provenance span with the
  wrong utterance as soon as a repair existed.

## What is deliberately not done yet

- **§8.6.5's second and fifth gates.** Unimplemented, failing closed, so no
  ASR adaptation can run until their conditions are transcribed from the design
  doc and the checks written. Every gate report names them.
- **The distilled classifier and router.** §8.6 places them after the ASR
  adaptation path, which is gated above.
- **DICOM.** Conditional on §4.4's trigger rule: ≥95% accession compliance
  after four weeks defers it indefinitely, <90% pursues it. Nothing to build
  until that measurement exists.
- **An actual RIS connection.** The messages are built and tested; nothing
  sends them. D22 — which field in upload metadata carries the accession
  number — is still open, and `build_oru` refuses rather than emitting a
  message with a placeholder.
- **An LLM-composed report.** Compose renders deterministically from
  `render_spec`. §7.9.3 lists `compose` as a task and plan §0.2a makes it a
  local-model candidate for Beta at the earliest — a model asked to write the
  report from structured fields smooths over the gaps that matter.
- **No bake-off results.** `eval/bakeoff.py` runs and is tested against stub
  engines, but the decision it exists to make needs audio from the new
  microphones. That is blocker #1 below, not a coding task.
- **No S5 admin panel screen.** Deferred from V1 (plan §3): the ranking
  accumulates and exports as CSV, which is enough for the pilot.
- **No PDF template import.** D16 says Word only. PDF is refused explicitly
  rather than half-parsed — a `template_field` mangled by text-layout
  extraction is invisible after import.
- **No model assignments seeded.** An assignment cannot go active without a
  gold-set `eval_run`, and seeding one would bypass the gate the registry
  exists to enforce.

- **No tenant offboarding cascade.** §6.11 defines erasure per *patient*, not
  per tenant. The lifecycle transition exists; the cascade behind it does not,
  and `registration.offboard` says so rather than pretending otherwise.
- **No manual fallback path.** §9.4 names the requirement — reports must still
  be produced when the pipeline is down — with no design. `suspended` logs the
  hook; the operator kill switch and documented degraded mode are still owed.

## Blockers that are not code

From plan §4. None of these are engineering tasks and several gate the
schedule:

1. **Microphones** — the longest-lead item. The `current` gold partition cannot
   accumulate until they are deployed, and it gates the bake-off, which gates
   the pipeline.
2. **D12 baseline audit** — 50 signed reports graded G0–G4, producing
   `baseline_cse_rate`. Every non-inferiority calculation depends on it and S7
   blocks without it. Pure human work on existing data; it can start today.
3. **Pooling clause + patient-notice wording** — the two genuinely irreversible
   items (§10.8). Free before contract #1, near-impossible to retrofit across
   signed labs. The schema is ready for them; the legal text is not written.
4. **D21 consent wording** — draft it as *two* consents (§10.3). Enrollment for
   diarization and training use are separate purposes under DPDP.
5. **D22 accession number field mapping** — without it, RIS filing at GA is
   impossible, independent of DICOM.
