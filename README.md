# radreport

Turns a radiologist's spoken dictation into a structured, checked draft report that a
radiologist reviews and signs, then exports as HL7 v2 ORU^R01 or FHIR R4. Many labs run on
one deployment, with each lab's rows walled off by Postgres row-level security.

> Here to evaluate the project rather than work on it? Start with **[RECRUITERS.md](RECRUITERS.md)**.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 + Alembic · Postgres 16 (pgvector, RLS) ·
Redis (optional) · S3 · Anthropic Claude · Kafka/Redpanda (optional) · Prometheus,
OpenTelemetry, Sentry · Docker · Render + Neon.

---

## Contents

- [Quick start](#quick-start)
- [How it fits together](#how-it-fits-together)
- [Hosting it](#hosting-it): [external services and keys](#1-external-services-and-the-keys-they-give-you), [deploying to Render](#2-deploying-to-render)
- [Day-to-day commands](#day-to-day-commands)
- [Configuration reference](#configuration-reference)
- [Code to change carefully](#code-to-change-carefully)
- [Status and known gaps](#status-and-known-gaps)
- [Other docs](#other-docs)

---

## Quick start

Requirements: Python 3.12, Docker (for Postgres 16 + pgvector and MinIO) and `make`.

```bash
make install                      # venv + editable install with dev deps
cp .env.example .env              # works as-is for local; every variable is commented
make up                           # Postgres 16 + pgvector on :5433, MinIO on :9000
make migrate-owner                # create the schema as the database OWNER (not the app role)
.venv/bin/python -m radreport.db.bootstrap --app-password <pw>   # the least-privileged app login
make seed-local                   # one account per role -> local-credentials.md (gitignored)
make dev                          # API on http://127.0.0.1:8000 with auto-reload
make worker                       # in a second terminal: runs the pipeline jobs
```

Then open:

| URL | What |
|---|---|
| `/admin/login` | Admin panel: labs, users, models, per-step model assignment, ops dashboards |
| `/ui/login` | Lab sign-in: review queue, signing, lexicon |
| `/features`, `/recruiter`, `/api-docs`, `/demo` | Public pages, no sign-in |
| `/docs` | OpenAPI (local/test/development only) |
| `/health`, `/ready` | Liveness (always 200, reports each dependency) and readiness (DB + migration head) |

To fill the demo lab with templates, past reports, dictations and signed drafts:
`.venv/bin/python -m radreport.devtools.demo_lab` (needs the server and a worker running).

**Two things that bite people:**

1. **The app must not connect as a superuser or the table owner.** Both bypass RLS, so the
   tenant-isolation tests would pass without proving anything. The app and the tests use
   `radreport_app_login`; only migrations run as the owner.
2. **Without Docker**, a local Postgres 16 needs the `vector`, `pgcrypto` and `citext`
   extensions. Homebrew's pgvector bottle only targets PG 17/18, so on PG 16 build it from source.

---

## How it fits together

```
 dictation ──► POST /ingest ──► S3 (audio) + job row ──► worker ──► pipeline (17 stages) ──► draft
                                                                                              │
 HL7 / FHIR export ◄── sign ◄── review UI (/ui) ◄─── review queue (riskiest first) ◄─────────┘
```

- **One Docker image, four processes** picked by `ops/docker/start.sh`:
  `web` (uvicorn), `worker` (job queue), `relay` (outbox → event bus), `jobs` (worker + relay
  in one container), plus `migrate` and `seed`.
- **Jobs** live in Postgres and are claimed with `FOR UPDATE SKIP LOCKED`; a dead worker's job
  is reclaimed after its visibility timeout and dead-lettered after its last attempt.
- **Events** (`recording.ingested`, `draft.ready`, `report.signed`, …) are written to an outbox
  in the same transaction as the change, so each consumer sees each event exactly once.
- **The pipeline** has deterministic safety stages (margin guard, self-correction handling,
  critical-findings alerting, grounding, verification, confidence) and model-backed stages that
  degrade explicitly when no model is assigned. Graph edges are defined only in
  `radreport/pipeline/v1.py`.
- **Access control** is one file: `radreport/api/access_policy.xml` lists every route, the roles
  allowed to call it, its rate limit, its size cap and every parameter. An unlisted route or an
  undeclared parameter stops the app from starting.
- **Models are configured, not hard-coded.** Providers, models and the model used for each
  pipeline step per lab are set in the admin panel. A provider row stores the *name* of the env
  var holding its key, never the key itself.
- **Review UI** is server-rendered with two small ES modules (focus timer, click-to-listen),
  with no JS build step.

Code map:

```
radreport/
  core/         config, tenancy rules, errors, logging, text utils
  db/           models (68 tables), migrations, sessions, shards, replica routing, first-start seed
  pipeline/     stage contracts, graph (v1.py), stages/, job runner
  adapters/     llm/ (prompt caching, k-sampling, registry, pricing) · asr/ · storage/
  ingest/       audio quality gates, capture-only ingest
  onboarding/   bringing a lab on board: roster, templates, corpus, lexicon, rules, readiness
  review/       queue, session, signing, grading, feedback, RBAC
  autonomy/     grant (Bayesian) and revocation (CUSUM)
  adaptation/   ASR adaptation gates
  eval/         harness, metrics, release gates, gold set, ASR bake-off
  export/       HL7 v2 ORU^R01, FHIR R4
  knowledge/    phonetics, synonyms/RadLex, languages
  admin/        admin auth, users, model config, CLI
  api/          FastAPI app, routes/, access policy, static/
  workers/ events/ cache/ monitoring/ observability
  devtools/     seed, demo lab, synthetic data, crash test, query report
ops/            docker/start.sh, pgbouncer, observability (Prometheus/Grafana), k6, cdn
```

[MODULES.md](MODULES.md) goes module by module; [API.md](API.md) is the HTTP contract.

---

## Hosting it

The production setup is **Render** (web service + background worker + Key Value, all from
`render.yaml`) with **Neon** for Postgres and **AWS S3** for audio.

### 1. External services and the keys they give you

#### Required

| Service | What to set up | Env var(s) it fills |
|---|---|---|
| **GitHub** | Push this repo to GitHub (it has no remote yet). Render deploys from it. | — |
| **Neon** (Postgres) | Create a project on **Postgres 16** in **AWS ap-southeast-1 (Singapore)**, the same region as the Render services. Copy the **direct** connection string of the owner role (turn *Connection pooling* **off** when copying it). The pooled URL rejects the startup options the migration step sets. `vector`, `pgcrypto` and `citext` are created by the first migration. | `RADREPORT_OWNER_DATABASE_URL` |
| **Render** | An account with a payment method: the web service and worker use a paid instance (`0.5c-512mb`), and the pre-deploy migration step only runs on paid services. Key Value (Redis) is the free plan. | Render generates `RADREPORT_APP_DB_PASSWORD`, `RADREPORT_LAB_AUTH__TOKEN_SECRET`, `RADREPORT_OBSERVABILITY__METRICS_TOKEN` and `RADREPORT_REDIS_URL` for you. |
| **You** | Choose the first product admin's password. | `RADREPORT_SEED_ADMIN_PASSWORD` |

Why not Render Postgres? Migration 0002 creates a `BYPASSRLS` role, which Render's
non-superuser database owner may not do. Neon's owner may.

#### Strongly recommended

| Service | What to set up | Env var(s) |
|---|---|---|
| **AWS S3** (audio) | 1. Create a bucket in `ap-southeast-1` with *Block all public access* on.<br>2. Create an IAM user with a policy allowing `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `arn:aws:s3:::<bucket>/*` and `s3:ListBucket` on `arn:aws:s3:::<bucket>`.<br>3. Create an access key for that user.<br>4. Optional: a KMS key for SSE-KMS (without one the app uses SSE-S3 and logs a warning). No CORS rule is needed: playback goes through presigned URLs in an `<audio>` element. | `RADREPORT_STORAGE__BUCKET`, `RADREPORT_STORAGE__REGION`, `RADREPORT_STORAGE__ACCESS_KEY_ID`, `RADREPORT_STORAGE__SECRET_ACCESS_KEY`, optionally `RADREPORT_STORAGE__SSE_KMS_KEY_ID` (add it by hand; it is not in `render.yaml`). Leave `RADREPORT_STORAGE__ENDPOINT_URL` **unset** for AWS. |
| **Demo logins** | The read-only `support` and `auditor` accounts shown on the sign-in pages and `/recruiter`. The value is in the gitignored `.env.render`. | `RADREPORT_DEMO_ACCOUNTS` (JSON) |

Without S3 the app still runs, but audio upload and playback return 503, and `/health` lists
storage under `fallbacks`.

#### Optional (each stays off until set)

| Service | Unlocks | Env var(s) |
|---|---|---|
| **Anthropic** (console.anthropic.com) | Claude models for the pipeline's model steps and template parsing. No model assignment is seeded, so nothing calls Claude until you register a provider and assign models in the admin panel. | `ANTHROPIC_API_KEY` |
| **Deepgram** | Reserved: `render.yaml` prompts for it, but no Deepgram adapter exists yet. Leave it blank. | `DEEPGRAM_API_KEY` |
| **Sentry** (free tier) | Error reports, scrubbed of bodies, cookies, messages and locals. | `RADREPORT_OBSERVABILITY__SENTRY_DSN` |
| **Grafana Cloud** (free tier) | Traces via OTLP; scrape `/metrics` with the generated metrics token; import `ops/observability/grafana/dashboards/radreport.json`. | `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` |
| **BioPortal** | RadLex lookups during onboarding. | `BIOPORTAL_API_KEY` |
| **Google Cloud Translation** | Online translation of unknown Hindi words (also enable it in System settings → Languages). | `GOOGLE_TRANSLATE_API_KEY` |
| **GitHub Actions** | `.github/workflows/synthetic-load.yml` runs k6 every 15 min against the live site, which keeps dashboards populated and the host awake. | Repo secrets `RADREPORT_BASE_URL`, `DEMO_ADMIN_EMAIL`, `DEMO_ADMIN_PASSWORD`, `DEMO_LAB`, `DEMO_LAB_EMAIL`, `DEMO_LAB_PASSWORD` (values in `.env.render`) |
| **Kafka / Redpanda** | Publish domain events to a real broker instead of in-process consumers. | `RADREPORT_EVENTS__BUS=kafka` + `RADREPORT_EVENTS__KAFKA_*` |
| **Cloudflare** | CDN / custom domain in front of Render; see `ops/cdn/cloudflare.md`. | `RADREPORT_TRUSTED_ORIGINS` if the public hostname differs from the one Render sees |

### 2. Deploying to Render

1. **Push the repo to GitHub.** Keep `.env`, `.env.render` and `local-credentials.md` out of it
   (all three are gitignored).
2. **Create the Neon database** and copy the direct owner connection string (see above). It
   looks like `postgresql://<owner>:<pw>@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
   `start.sh` rewrites `postgresql://` to the psycopg driver itself.
3. **Create the S3 bucket and IAM key** (see above).
4. **Render dashboard → New → Blueprint →** connect the GitHub repo. Render reads `render.yaml`
   and plans three resources: `radreport-web`, `radreport-jobs` and `radreport-cache`.
5. **Fill in the prompted values** on `radreport-web`. The worker copies them from the web
   service, so you enter each one once:
   - `RADREPORT_OWNER_DATABASE_URL`: Neon direct owner URL
   - `RADREPORT_STORAGE__BUCKET`, `__REGION` (`ap-southeast-1`), `__ACCESS_KEY_ID`, `__SECRET_ACCESS_KEY`
   - `RADREPORT_SEED_ADMIN_PASSWORD`: the first admin's password
   - `RADREPORT_DEMO_ACCOUNTS`: from `.env.render`
   - `ANTHROPIC_API_KEY`: optional; `DEEPGRAM_API_KEY`: leave blank
6. **Apply.** On each deploy Render:
   - builds the image from `Dockerfile`;
   - runs the pre-deploy command `radreport-start migrate`, which applies every migration as
     the Neon owner and then creates or updates the `radreport_app_login` role with the
     generated `RADREPORT_APP_DB_PASSWORD`;
   - starts the web service, which connects as `radreport_app_login` and, on an **empty**
     database, seeds the model catalog, `admin@radreport.local`, the demo logins and a demo lab;
   - starts `radreport-jobs` (job worker + outbox relay).
7. **Check it's up:**
   ```bash
   curl -s https://<service>.onrender.com/ready  | jq   # {"status": "ready", ...}; this is Render's health check
   curl -s https://<service>.onrender.com/health | jq   # each dependency, plus any "fallbacks" in use
   ```
8. **Sign in** at `/admin/login` as `admin@radreport.local` with `RADREPORT_SEED_ADMIN_PASSWORD`,
   then open `/recruiter` and `/features` to check the public pages.
9. **Optional follow-ups:**
   - Add the GitHub Actions secrets for synthetic load.
   - Assign Claude models to pipeline steps per lab in the admin panel. An assignment stays
     *proposed* until it has a gold-set evaluation run.
   - Point Grafana Cloud / Sentry at the service (see the optional table).
   - Add a custom domain in Render (and Cloudflare, if you use it).

**Operating the deployment:**

| Task | How |
|---|---|
| Re-sync demo logins after changing `RADREPORT_DEMO_ACCOUNTS` | Render Shell on `radreport-web`: `radreport-start seed` |
| Reset an admin's password | Render Shell: `radreport-start python -m radreport.admin.cli set-password --email <email>` |
| Run migrations by hand | Render Shell: `radreport-start migrate` |
| Scale | Raise `WEB_CONCURRENCY` / `WORKER_CONCURRENCY`, but keep processes × (`POOL_SIZE` + `MAX_OVERFLOW`) under Neon's `max_connections` (≈100 on the smallest compute), or add PgBouncer |
| Split worker and relay | Replace `radreport-jobs` with two workers running `radreport-start worker` and `radreport-start relay` |

`RADREPORT_SEED_ADMIN_PASSWORD` is only read the first time the database is seeded. Changing it
later does nothing; reset the password with the admin CLI instead.

---

## Day-to-day commands

`make help` lists every target.

| Area | Command | Notes |
|---|---|---|
| Server | `make dev` | Foreground, auto-reload |
| | `make run` / `stop` / `restart` / `status` / `logs` | Background, 2 workers, waits for `/ready`. `make run PORT=9000 WORKERS=4` |
| Workers | `make worker CONCURRENCY=2` | Pipeline jobs + periodic jobs (stuck-job reclaim, partitions, cost scan, eval refresh, new-terms scan) |
| | `make relay` | Publish committed outbox events |
| DB | `make migrate-owner` | **Use this one.** Some migrations create roles and grants that the app role may not |
| | `make revision M="what changed"` | Autogenerate a migration |
| | `RADREPORT_DATABASE_URL=<url> .venv/bin/alembic current` | Where a database is |
| Tests | `make test` / `make test-unit` / `make check` | `check` = lint + tests, what CI runs |
| | `.venv/bin/pytest -q -k "rover or cusum"` | By name |
| Admin | `make seed`, `make admin EMAIL=…`, `make admin-password EMAIL=…` | First admin from code; later ones from the admin panel's Users page |
| Demo | `make seed-local`, `python -m radreport.devtools.demo_lab [--only review] [--force]` | Set `RADREPORT_STORAGE__BACKEND=local` if you have no S3/MinIO |
| Resilience | `RADREPORT_TEST_DATABASE_URL=<…_test> make crash-test` | Kills workers, crashes the relay, races jobs, floods a route; writes `docs/CRASH_TEST.md` |
| Media | `make gifs` | Records `docs/media/*.gif|mp4` from the running app |
| Observability | `docker compose --profile observability up -d` | Prometheus :9090, Grafana :3000, Jaeger :16686 |
| Load | `k6 run ops/k6/synthetic.js -e BASE_URL=http://127.0.0.1:8000` | |
| Scale-out | `make pgbouncer` + `RADREPORT_DB__PGBOUNCER=true` | Needed beyond ~2 workers locally |
| Housekeeping | `make fmt`, `make lint`, `make clean`, `make down` | |

**Tests and the database:** DB-backed tests skip unless `RADREPORT_TEST_DATABASE_URL` is set,
and it must point at `radreport_app_login`, not the owner:

```bash
export RADREPORT_TEST_DATABASE_URL=postgresql+psycopg://radreport_app_login:<pw>@localhost:5433/radreport_test
```

---

## Configuration reference

Settings come from the environment and `.env`, prefixed `RADREPORT_`, with `__` for nesting
(`RADREPORT_STORAGE__BUCKET`). `.env.example` documents every variable; these are the ones you
will actually touch.

| Variable | Notes |
|---|---|
| `RADREPORT_DATABASE_URL` | The app's connection. **Must be the non-owner role.** On Render it is derived from the owner URL + `RADREPORT_APP_DB_PASSWORD`. |
| `RADREPORT_OWNER_DATABASE_URL` | Container only: used by `migrate` and to derive the app URL. |
| `RADREPORT_ENVIRONMENT` | `local`/`test`/`development` serve `/docs` and send the admin cookie without `Secure`; anything else hides docs and requires HTTPS. |
| `RADREPORT_LAB_AUTH__TOKEN_SECRET` | Signs lab access tokens. Required outside local/test/development (32+ chars). |
| `RADREPORT_STORAGE__*` | S3-compatible audio store. `BACKEND=local` writes to `.storage/` (dev only). |
| `RADREPORT_REDIS_URL` | Shared cache (lab config, roles, model assignments, admin sessions). Unset: per-worker memory cache. |
| `RADREPORT_DB__POOL_SIZE`, `__MAX_OVERFLOW` | Per process. Default 30 + 10, which is too many for a small managed DB. |
| `RADREPORT_DB__REPLICA_URL`, `RADREPORT_DB__SHARDS` | Read replica (lag-aware fallback) and consistent-hash shards. |
| `RADREPORT_LLM__RESPONSE_CACHE` | `postgres` (default), `shared` (Redis) or `off`. |
| `RADREPORT_EVENTS__BUS` | `postgres` (default) or `kafka`. An unreachable Kafka falls back to postgres. |
| `RADREPORT_TRUSTED_ORIGINS` | JSON list of extra origins allowed to send admin writes (CSRF). |
| `RADREPORT_SEED_ON_START`, `RADREPORT_SEED_ADMIN_PASSWORD` | First-start seed of an empty database. |
| `RADREPORT_DEMO_ACCOUNTS` | `[{"label","email","password","role","lab"}]`. Only `support` and `auditor` are ever displayed. |
| `ANTHROPIC_API_KEY`, `BIOPORTAL_API_KEY`, … | **Unprefixed and not settings.** Each provider row names the variable it reads, so a second account is a new variable plus a new provider row, with no code change. |

---

## Code to change carefully

Each of these has a test that fails loudly. [MODULES.md](MODULES.md) explains why each one is
built the way it is.

| Where | The rule |
|---|---|
| `core/tenancy.py` | Every table has `tenant_id` or is on the exception list; migration 0002 generates RLS policies from the same introspection. |
| `db/base.py:tenant_fk` | Use it (composite FK) between two tenant-scoped tables, never a bare `ForeignKey`. |
| `api/access_policy.xml` | The only place route roles and parameters are decided; the app won't start if it disagrees with the routes. |
| `api/deps.py:admin_lab_session` | Admin actions on a lab must bind the session to that lab, or RLS silently returns nothing. |
| `adapters/llm/prompt.py` | Prompts are ordered stable → cache breakpoint → volatile; the wrong order can't be expressed. |
| `adapters/llm/sampling.py` | Sample 1 completes before samples 2..k fire (prompt-cache warm-up). |
| `adapters/llm/registry.py` | No model activation without a gold-set `eval_run`. |
| `pipeline/stages/normalise.py` | Margin guard escalates instead of guessing; it never rewrites the transcript (offsets are provenance). |
| `pipeline/stages/repairs.py` | Retraction applies backwards, correction forwards; a repair inside an utterance splits it. |
| `pipeline/stages/critical.py` | Negation is sentence-local and positional. |
| `pipeline/stages/grounding.py` | The quote is checked against the cited character range, not searched for. |
| `pipeline/stages/confidence.py` | `min(critical) × mean(all)`; averaging would hide one weak critical field. |
| `pipeline/stages/post_correction.py` | The only stage allowed to rewrite the transcript, and only before any offset exists. |
| `pipeline/stages/persist.py` | Utterance rows are keyed on their own `seq`; inserts are in reference order. |
| `core/text.py:split_sentences` | A period between digits is not a sentence boundary. |
| `adapters/asr/rover.py` | NULL is a voting candidate, so a word only one engine heard loses. |
| `review/rbac.py`, `review/signing.py` | An assistant revises, a radiologist signs; `preflight` mirrors every signing gate. |
| `review/session.py` | Edit time is browser focus time, clamped to wall clock by the server. |
| `review/grading.py:_feed_autonomy` | The only place autonomy accrual and the CUSUM are fed. |
| `autonomy/` | Granting is deliberately hard, revocation mechanical. |
| `adaptation/gates.py` | G2 and G5 are unimplemented and fail closed. |
| `export/hl7.py:escape` | Escape the escape character first. |
| `api/app.py` `/health` vs `/ready` | Liveness checks only the process; readiness checks DB and migration head. |

---

## Status and known gaps

Built: ingest, the full pipeline, review and signing, HL7/FHIR export, lab onboarding, ROVER
reconciliation, the LLM critic, autonomy grant/revocation, drift monitoring, the admin panel,
observability and the crash test.

Not done, deliberately and visibly:

- **No field extraction in the job runner yet.** `pipeline/runner.py` builds the graph with no
  model client, so drafts from uploads have their template and alerts but no field values.
  Wiring it needs the lab's assigned extraction model resolved in `default_graph_factory`.
- **No ASR engine chosen.** The stub engine is used; the choice waits on the ASR bake-off,
  which needs audio from the new microphones.
- **Two adaptation gates (G2, G5) are unimplemented** and fail closed; their criteria aren't in
  the source spec.
- **No RIS connection.** Messages are built and tested but not sent; the accession-number field
  mapping (D22) is still open.
- **No DICOM, no PDF template import, no tenant offboarding cascade, no manual fallback path.**
- **Language dictionaries** (Hindi, French, Spanish) still need review by a pilot lab's radiologists.
- **Production numbers** (DB CPU at peak, real cost, replica share) need production traffic;
  local measurements are in [PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md).

Non-code blockers: microphone deployment, the baseline audit of 50 graded reports, the legal
text for the pooling clause, patient notice and consents, and the accession-number mapping.

---

## Other docs

| File | For |
|---|---|
| [RECRUITERS.md](RECRUITERS.md) | A 5-minute tour for non-developers |
| [FEATURES.md](FEATURES.md) | Product features; rendered at `/features` and `/recruiter` (don't move it) |
| [API.md](API.md) | HTTP contract; rendered at `/api-docs` (don't move it) |
| [MODULES.md](MODULES.md) | Module-by-module guide to `radreport/` |
| [PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md) | Measured query counts, latencies, load test |
| [docs/](docs/) | Crash test report, languages, synonyms, template model, fine-tuning |
| [ops/cdn/cloudflare.md](ops/cdn/cloudflare.md) | Putting Cloudflare in front |
