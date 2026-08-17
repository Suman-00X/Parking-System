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

