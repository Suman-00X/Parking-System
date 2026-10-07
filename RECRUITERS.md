# RadReport: a quick tour for recruiters

**What it is:** a platform that turns a radiologist's spoken dictation into a finished,
structured medical report. It checks its own work and flags anything risky, and it never
lets an unchecked report out without a doctor's signature.

**Who built it:** Suman, end to end: system design, backend, database, the AI pipeline,
security, the review screens, testing, observability and deployment.

---

## Try it in two minutes

The live demo has read-only logins, so you can click anywhere without breaking anything.

1. Open the live site's **`/recruiter`** page. It has the demo logins, screen recordings, the
   system design and the crash-test results in one place.
2. Sign in to the **admin panel** (`/admin/login`) with the *support* login to see how labs,
   users and AI models are managed.
3. Sign in to a **lab** (`/ui/login`) with the *auditor* login to see the review queue and
   signed reports.
4. Browse **`/features`** for the product story and **`/api-docs`** for the HTTP API.

No time to click? The recordings are in [`docs/media/`](docs/media/): `features-tour`,
`demo-sign-in` and `admin-panel`.

---

## The problem, in one paragraph

Radiologists dictate their findings, and someone types them up into the hospital's format.
That is slow and error-prone, and an urgent finding can sit unnoticed in a queue. Off-the-shelf
speech-to-text mishears medical words, sometimes "hears" words nobody said, and writes down
both a slip and its correction ("left — sorry, right"). In radiology, that slip is often
the side of the body.

## What RadReport does about it

- **Writes the report in the lab's own template**, field by field, from natural speech.
- **Ties every sentence to the second of audio it came from.** One click plays it, and anything
  that can't be traced back is dropped and flagged rather than guessed.
- **Raises an alert on urgent findings** (collapsed lung, brain bleed) using fixed rules rather
  than AI, so the check behaves the same way every time. It understands that "no fracture" is
  not a fracture.
- **Puts the riskiest fields first** for the reviewing doctor, and requires a radiologist's
  signature. An assistant can edit but never sign.
- **Exports to hospital systems** in the two standard formats, HL7 v2 and FHIR R4.
- **Serves many labs from one system**, with each lab's data walled off at the database level.

---

## By the numbers

| | |
|---|---|
| **17** | automated steps between "doctor speaks" and "report ready" |
| **11** | AI-powered steps, each assignable to a different model per lab |
| **68** | database tables, each lab's rows isolated by the database itself |
| **1,100+** | automated tests, including tests that try to read one lab's data from another |
| **9 of 9** | deliberate failures survived in the crash test (killed workers, lost database, floods) |
| **500** | simultaneous users in the load test, with 99.99% of requests served |
| **~31,000** | lines of code |

---

## Engineering highlights

| Area | What was done |
|---|---|
| **Multi-tenant security** | Postgres row-level security on every lab table, composite foreign keys so one lab's row can't point at another's, and tests that try to break the isolation. |
| **Access control** | Every route's allowed roles, rate limit and accepted parameters live in one policy file. The app refuses to start if any route is missing from it. |
| **AI done carefully** | Several models are configurable per lab and per step. Every value must quote its source, a second model critiques each draft, prompts are ordered so caching cuts cost, and no model goes live without an evaluation run. |
| **Reliability** | A Postgres job queue that survives worker crashes, a transactional outbox for exactly-once events, and graceful fallbacks when Redis, Kafka or S3 are missing. |
| **Safety statistics** | Report types earn "no review needed" status only with Bayesian evidence, and lose it automatically when a CUSUM quality monitor trips. |
| **Scale** | Connection pooling (PgBouncer), a lag-aware read replica, consistent-hash sharding and a shared Redis cache. |
| **Observability** | Prometheus metrics, a Grafana dashboard, OpenTelemetry traces, Sentry errors with patient data scrubbed, and k6 synthetic traffic in CI. |
| **Deployment** | One Docker image for every process, deployed as a Render Blueprint with Neon Postgres and AWS S3. Migrations run as a non-superuser owner, as managed databases require. |

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 + pgvector, Redis,
AWS S3, Anthropic Claude, Kafka/Redpanda, Prometheus, Grafana, OpenTelemetry, Sentry, k6,
Docker, Render, Neon, GitHub Actions.

---

## Honest about what's left

Some parts are deliberately unfinished, and the system says so instead of hiding it. The
speech engine waits on a real-audio bake-off, two safety gates stay locked until their exact
criteria are written down, and the connection to a live hospital system waits on one data-mapping
decision. These are listed in the [README](README.md#status-and-known-gaps).

---

## Where to look next

- **[FEATURES.md](FEATURES.md)**: every feature, each with the reasoning behind it
- **[docs/CRASH_TEST.md](docs/CRASH_TEST.md)**: what happened when the system was broken on purpose
- **[PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md)**: measured performance and the load test
- **[README.md](README.md)**: the developer guide, including how it is hosted
