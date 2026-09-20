# HTTP API

**Ordered the way the product runs, not the way the code is filed.** Read it
top to bottom and you walk a lab from "is the service up" to "it signs reports
without a human". Each phase only uses what the phases before it created.

[MODULES.md](MODULES.md#http-routes) groups the same routes by source file —
use that when you already know where you are going.
[Appendix A](#appendix-a--index-by-prefix) is the flat lookup table, with the
realm, roles, rate limit and body cap of every route.

| Phase | What happens | Routes | Prefixes |
|---|---|---:|---|
| [I](#part-i--ground-rules) | Access policy, auth, scoping, roles, errors — plus admin sign-in and platform users | 14 | `/admin`, `/admin/api` |
| [0](#phase-0--is-the-service-up) | Is the service up? | 6 | — |
| [1](#phase-1--create-a-lab-and-its-people) | A lab exists, with people in it | 11 | `/admin`, `/admin/api`, `/onboarding` |
| [2](#phase-2--teach-the-lab-its-knowledge) | The lab's templates, vocabulary and rules are learned | 34 | `/onboarding`, `/admin`, `/admin/api` |
| [3](#phase-3--runtime-one-report-end-to-end) | A dictation becomes a signed report | 12 | `/ingest`, `/review`, `/ui` |
| [4](#phase-4--operate-it) | Measure it, export it, automate it | 16 | `/review`, `/ga`, `/admin/api` |

**93 routes** — every one listed in
[`api/access_policy.xml`](radreport/api/access_policy.xml): 87 on routers,
`/health` and `/ready` on the app, and FastAPI's four documentation routes.
Most return JSON; the admin panel's pages under `/admin` (but not `/admin/api`)
and `/ui` serve HTML and `303` redirects, and three return neither (audio,
HL7).

The admin panel is **not** a separate phase. A product admin configures a lab
*during* onboarding and operates it *during* runtime, so `/admin` and
`/admin/api` routes appear inside the phase where they are actually used.

> **Line numbers point at the `@router` decorator** and were taken from the
> filesystem on **2026-10-06**. They drift. Regenerate with:
>
> ```sh
> grep -rn '@router\.\(get\|post\|put\|patch\|delete\)' radreport/api/routes/
> ```

OpenAPI is served by FastAPI at `/docs` and `/openapi.json` — in the `local`,
`test` and `development` environments only; elsewhere they are `404`.

### Contents

- [Part I — Ground rules](#part-i--ground-rules) · [access policy](#the-access-policy) · [auth](#authentication) · [scoping](#tenant-scoping) · [roles](#roles) · [errors](#errors) · [platform users](#platform-users)
- [Phase 0 — Is the service up?](#phase-0--is-the-service-up)
- [Phase 1 — Create a lab and its people](#phase-1--create-a-lab-and-its-people)
- [Phase 2 — Teach the lab its knowledge](#phase-2--teach-the-lab-its-knowledge)
- [Phase 3 — Runtime: one report, end to end](#phase-3--runtime-one-report-end-to-end)
- [Phase 4 — Operate it](#phase-4--operate-it)
- [Appendix A — Index by prefix](#appendix-a--index-by-prefix)
- [Appendix B — Known gaps](#appendix-b--known-gaps)

---

# Part I — Ground rules

Five things decide whether any request in any phase is allowed. Learn these
once; every phase below assumes them.

## The access policy

Every route, and who may call it, is declared in one file:
[`api/access_policy.xml`](radreport/api/access_policy.xml). It is read once at
startup by [`load_policy`](radreport/api/access.py#L191), and
[`AccessMiddleware`](radreport/api/access.py#L297) checks every request against
it **before any route handler runs**.

Each `<route>` names a method, a path, a realm, the roles allowed to call it, a
rate limit and, for anything that takes a body, a maximum body size:

```xml
<route id="admin.lab.status" method="POST" path="/admin/labs/{tenant_id}/status"
       rate-limit="admin-write" max-body-bytes="4096">
  <allow role="product_admin"/>
</route>
```

The middleware, in order:

| Step | Refusal |
|---|---|
| Match the method and path to a `<route>`. Unlisted, or limited by `environments` to other deployments (the docs routes) | `404 Not Found` |
| Compare the declared `Content-Length` with `max-body-bytes` | `413` |
| Count the request against an IP-keyed rate limit | `429` with `Retry-After` |
| Identify the caller for the route's realm (below) | `401`, or a `303` to `/admin/login` |
| Count it against a caller-keyed rate limit | `429` with `Retry-After` |
| Check the caller's role is one the route allows | `403` |

The identified caller is left on `request.state.identity`; handlers read it
through [`current_admin`](radreport/api/deps.py#L39) and
[`current_principal`](radreport/api/deps.py#L50).

**The app refuses to start if the policy and the routes disagree.**
[`verify_coverage`](radreport/api/access.py#L210) fails `create_app()` when a
served route is not in the file, or the file lists a route that is not served.
A new route without a policy entry cannot ship by accident.

**Adding a role or a permission is an edit to the XML**, not to code: declare
the `<role>` under its realm, and add an `<allow role="..."/>` to each route it
may call. A role must belong to the route's realm, so an admin role can never be
allowed onto a lab route or the reverse — the parser refuses the file.

Rate limits are named `<rate-limit>` elements; their counter is shared by every
route that names them, per caller (`key="principal"`) or per client address
(`key="ip"`):

| Limit | Requests | Window | Keyed by |
|---|---:|---:|---|
| `login` | 5 | 60 s | IP |
| `public` | 120 | 60 s | IP |
| `admin-read` | 300 | 60 s | caller |
| `admin-write` | 60 | 60 s | caller |
| `admin-upload` | 10 | 60 s | caller |
| `lab-read` | 300 | 60 s | caller |
| `lab-write` | 120 | 60 s | caller |
| `lab-upload` | 30 | 60 s | caller |

The counter is a sliding window held in the process's own memory
([`RateLimiter`](radreport/api/access.py#L221)), so with several workers each
one counts separately.

## Authentication

Three realms, and they never mix. `public` routes need nothing. A lab user
belongs to exactly one tenant; a product admin belongs to none. Which realm a
route is in decides which credentials are even looked at — an admin cookie on a
`/review` request is ignored, and lab headers on an `/admin` request are
ignored.

### Lab users — `app_user`

```
X-User-Id:   <app_user uuid>
X-Tenant-Id: <tenant uuid>
```

Both are required. Missing either is `401 missing principal headers`; a value
that is not a UUID is `401 malformed principal headers`
([`_identify_lab_user`](radreport/api/access.py#L361)).

The middleware then looks the user up **inside that tenant**, under its RLS
binding. An unknown user, an inactive one, or one who belongs to a different
tenant is `401 unknown or inactive user`. The user's **stored** roles are
checked against the route's `<allow>` list — `403` if none match. Handlers keep
their own finer checks on top (a radiologist-only gate, say).

**This is still not real authentication.** Anyone who knows a user id and its
tenant id can act as that user. It is a development placeholder, replaceable
without touching the routes.

### Product admins — `platform_user`, by session cookie

The admin realm accepts one credential: the `radreport_admin` cookie set by
`POST /admin/login`. It is `httponly`, `samesite=lax`, `secure` outside
`local`/`test`/`development`, and lasts 12 hours. There are no admin headers.

| Method | Path | Who | Form fields | Source |
|---|---|---|---|---|
| GET | `/admin/login` | public | — | [`admin_panel.py:143`](radreport/api/routes/admin_panel.py#L143) |
| POST | `/admin/login` | public — rate limit `login`, 5 a minute per IP | `email`, `password` | [`admin_panel.py:168`](radreport/api/routes/admin_panel.py#L168) |
| POST | `/admin/logout` | public — reads the cookie if there is one | — | [`admin_panel.py:192`](radreport/api/routes/admin_panel.py#L192) |
| GET | `/admin` | `product_admin`, `support` — redirects to `/admin/labs` | — | [`admin_panel.py:202`](radreport/api/routes/admin_panel.py#L202) |

Without a valid session ([`_identify_admin`](radreport/api/access.py#L349)):

- a **browser `GET` of a panel page** is a `303` to `/admin/login`;
- **anything under `/admin/api/*`, and every `POST`**, is
  `401 sign in at /admin/login`.

Login failure gives one message for an unknown account, a wrong password and an
inactive account alike — telling them apart turns the login page into an
account enumerator. A successful login redirects to `/admin/labs`. When demo
accounts are configured, the login page shows them on a *Test credentials* tab.

`logout` is idempotent: an unknown or already-revoked token is not an error.

**Forced sign-out.** Deactivating an account or resetting its password from the
[Users page](#platform-users) ends all of its sessions.
`python -m radreport.admin.cli revoke-sessions --email ...` signs an account
out everywhere without changing anything else.

### Two doors to every admin operation

The admin panel is served twice, over the same functions:

- **Pages** under `/admin/*` — HTML, forms, and `303` redirects that carry the
  outcome as `?error=` or `?notice=` (URL-encoded).
- **JSON** under `/admin/api/*` — the same operations for scripts and tests,
  with ordinary status codes.

Both take the same cookie. A script signs in with `POST /admin/login` and keeps
the cookie.

## Tenant scoping

Every tenant-scoped route opens a database session bound to exactly one tenant
so row-level security can do its job.

| Principal | How the tenant is chosen | Opened by |
|---|---|---|
| Lab user | `X-Tenant-Id`, after the middleware has verified the user belongs to it | [`get_db`](radreport/api/deps.py#L61) |
| Product admin | The `{tenant_id}` in the route's path — `/admin/labs/{tenant_id}/...`, `/admin/api/labs/{tenant_id}/...` | [`admin_lab_session`](radreport/api/deps.py#L72) / [`get_admin_lab_db`](radreport/api/deps.py#L87) |

A `{tenant_id}` naming a lab that does not exist is `404 no lab ...`.

**Selecting a lab is audited.** Every admin request that opens a lab's session
writes an `admin_org_selected` audit row
([`select_org`](radreport/db/session.py#L86)) with the admin's id and address.

The routes that are inherently cross-tenant — listing labs, registering one,
providers and models, platform users — go through
[`system_session()`](radreport/db/session.py#L65) instead and take no
`{tenant_id}`.

## Roles

Roles are read from the stored row — never from the request.

| Realm | Role | What the policy lets it call |
|---|---|---|
| admin | `product_admin` | Everything in the admin realm |
| admin | `support` | Only the admin realm's `GET` routes — a read-only view of the panel and `/admin/api` |
| lab | `radiologist` | Every clinical gate: template review, merge decisions, batch apply/revert, mapping verification, collision resolution, boilerplate promotion, rule authoring and approval, signing, addenda, grading; plus capture, the queue and the reads |
| lab | `transcriptionist` | Verbatim transcript submission; the non-radiologist half of the review queue |
| lab | `lab_admin` | Capture, voice enrollment and training consent, the onboarding reads, autonomy revocation, export, drift |
| lab | `auditor` | Read-only: queue, drafts, usefulness, autonomy and coverage, drift |

`app_user.roles` values are in [`core/types.py`](radreport/core/types.py#L35);
`platform_user.role` is [`PlatformRole`](radreport/core/types.py#L44).

The per-route list is the policy file, and it is reproduced in
[Appendix A](#appendix-a--index-by-prefix). The finer checks still live next to
the routes they guard:
[`_require_role`](radreport/api/routes/onboarding.py#L35) and
[`_uploader`](radreport/api/routes/onboarding.py#L45) (`/onboarding`),
[`_reviewer`](radreport/api/routes/review.py#L33) (`/review`), and
[`_require_lab_role`](radreport/api/routes/ga.py#L35) (`/ga`).

The admin panel reads the same file to decide what to draw: a `support` account
sees the pages without the forms and buttons it could not use.

**Product admins are locked out of clinical decisions by construction.** Every
clinical route is in the lab realm, and no admin role can be allowed there. The
reverse holds too: data upload, the mining steps, autonomy *grants* and the
adaptation gates are admin-realm only — `a lab must not grant itself autonomy
over its own reports`.

## Errors

FastAPI's `{"detail": ...}` envelope throughout, including the middleware's own
refusals. `detail` is a string except where noted. Domain errors are mapped to
status codes in each route file — for example
[`_refused`](radreport/api/routes/admin_api.py#L42) in the admin API and
[`_guard`](radreport/api/routes/review.py#L46) for the review surface. The
admin panel's pages never show a status code for a refused action: they
redirect back with the reason in `?error=`.

| Code | Meaning here |
|---|---|
| `303` | A panel page or form: either the outcome redirect, or a signed-out browser sent to `/admin/login` |
| `401` | Missing or malformed lab headers, an unknown or inactive lab user, or no valid admin session |
| `403` | The caller's role is not one the route allows, or a handler's finer check refused |
| `404` | A route not in the policy, a docs route outside development, or no such row **in this tenant** — cross-tenant reads present as absent |
| `409` | State conflict: duplicate audio, illegal status transition, blocked batch, signing refused, duplicate account |
| `412` | A gate said no: ungated model activation, autonomy grant without evidence, export without an accession, adaptation gates failing |
| `413` | Declared body larger than the route's `max-body-bytes` |
| `422` | Validation, or a domain rule refusing the content |
| `429` | Over a rate limit; `Retry-After` says how many seconds to wait |
| `503` | Object store unreachable (`/review/drafts/{id}/audio`) |

`409` vs `412` is deliberate. `409` means *the thing is in the wrong state*;
`412` means *the evidence required to proceed does not exist yet*. A client
should retry a `409` after changing state, and must not retry a `412` without
new evidence.

`404` on a tenant-scoped row is indistinguishable from a cross-tenant access
attempt. That is the point.

## Platform users

Who can sign in to the admin panel, and in which role. Product admins add and
retire `product_admin` and `support` accounts here; a `support` account can see
the list but change nothing.

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/admin/users` | `product_admin`, `support` — **HTML** | [`admin_panel.py:634`](radreport/api/routes/admin_panel.py#L634) |
| POST | `/admin/users` | `product_admin` | [`admin_panel.py:689`](radreport/api/routes/admin_panel.py#L689) |
| POST | `/admin/users/{user_id}/deactivate` | `product_admin` | [`admin_panel.py:710`](radreport/api/routes/admin_panel.py#L710) |
| POST | `/admin/users/{user_id}/reactivate` | `product_admin` | [`admin_panel.py:715`](radreport/api/routes/admin_panel.py#L715) |
| POST | `/admin/users/{user_id}/password` | `product_admin` | [`admin_panel.py:720`](radreport/api/routes/admin_panel.py#L720) |
| GET | `/admin/api/users` | `product_admin`, `support` | [`admin_api.py:303`](radreport/api/routes/admin_api.py#L303) |
| POST | `/admin/api/users` | `product_admin` | [`admin_api.py:317`](radreport/api/routes/admin_api.py#L317) |
| POST | `/admin/api/users/{user_id}/deactivate` | `product_admin` | [`admin_api.py:335`](radreport/api/routes/admin_api.py#L335) |
| POST | `/admin/api/users/{user_id}/reactivate` | `product_admin` | [`admin_api.py:341`](radreport/api/routes/admin_api.py#L341) |
| POST | `/admin/api/users/{user_id}/password` | `product_admin` | [`admin_api.py:351`](radreport/api/routes/admin_api.py#L351) |

Create takes `email`, `display_name`, `role` (`product_admin` \| `support`) and
`password` (at least 12 characters) — as form fields on the page, as JSON on the
API. Password reset takes `password`. The JSON routes return the account:
`id`, `email`, `display_name`, `role`, `is_active`, `last_login_at`.

The rules live in [`admin/users.py`](radreport/admin/users.py):

- **You cannot deactivate your own account**, and **you cannot deactivate the
  last active product admin** — add another first.
- **Deactivation and password reset end every live session** of that account.
  Resetting your own password from the panel signs you out and sends you back to
  the login page.
- Every change writes an audit row (`platform_user_created`,
  `platform_user_deactivated`, `platform_user_reactivated`,
  `admin_password_set`).

On the API, a duplicate email is `409`, an unknown user `404`, and every other
refusal — bad role, missing email or name, a short password, deactivating
yourself or the last product admin — `422` with the reason.

### The first product admin

Signing in needs an account and creating one needs a session, so the first
product admin is created from a shell, by whoever already has the database:

| Command | What it does |
|---|---|
| `RADREPORT_SEED_ADMIN_PASSWORD=... make seed` | Seeds the model catalog and creates `admin@radreport.local` as `product_admin` with that password ([`seed_platform_admin`](radreport/devtools/seed.py)). `python -m radreport.devtools.seed --admin-email you@example.com` picks the address. Without the variable the account is created with no password, and the seed says how to set one |
| `make admin EMAIL=you@example.com` | Creates a product admin, prompting for the password ([`radreport.admin.cli create`](radreport/admin/cli.py)) |
| `make admin-password EMAIL=you@example.com` | Sets or resets a password and revokes that account's sessions (`cli set-password`) |

Every later account is added from the Users page.

---

# Phase 0 — Is the service up?

No authentication. Two routes, and the distinction between them matters.

| Method | Path | Returns | Source |
|---|---|---|---|
| GET | `/health` | `{"status": "ok", "environment": "..."}` — liveness only | [`app.py:48`](radreport/api/app.py#L48) |
| GET | `/ready` | `200` with `{"status": "ready", "checks": {...}}`, or `503` | [`app.py:53`](radreport/api/app.py#L53) |

`/ready` checks the database connection **and** that the Alembic revision
matches the code's head:

```json
{"status": "ready", "checks": {"database": "ok", "migrations": "at 0005"}}
```

`/health` returning `200` against an unreachable database was a real bug. A
process running against a schema older than its code is `not_ready`, because
that failure otherwise surfaces as random column errors hours later. `make run`
waits on `/ready`, not `/health`.

FastAPI's documentation routes are also public, but exist only in `local`,
`test` and `development`: `GET /openapi.json`, `GET /docs`,
`GET /docs/oauth2-redirect`, `GET /redoc`.

---

# Phase 1 — Create a lab and its people

**11 routes** (plus the sign-in routes in
[Part I](#product-admins--platform_user-by-session-cookie)). Nothing clinical
happens here. At the end of this phase a tenant row exists, it is in
`onboarding`, and its staff are in `app_user` with roles.

## 1.1 Register the lab

Two doors to the same operation, with the same fields.

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/admin/labs` | `product_admin`, `support` — the lab list page | [`admin_panel.py:207`](radreport/api/routes/admin_panel.py#L207) |
| POST | `/admin/labs` | `product_admin` — the registration form | [`admin_panel.py:269`](radreport/api/routes/admin_panel.py#L269) |
| GET | `/admin/labs/{tenant_id}` | `product_admin`, `support` — one lab's page | [`admin_panel.py:285`](radreport/api/routes/admin_panel.py#L285) |
| POST | `/admin/api/labs` | `product_admin` | [`admin_api.py:80`](radreport/api/routes/admin_api.py#L80) |

Body for `POST /admin/api/labs` (form fields of the same names on the page):

| Field | Notes |
|---|---|
| `name` | required |
| `slug` | required, `^[a-z0-9][a-z0-9-]{1,62}$`, checked on the server for the form too; a taken slug is `409` |
| `admin_email`, `admin_display_name`, `admin_employee_code` | the first lab admin, created with it |
| `training_pooling_consent` | default `false` |
| `training_consent_ref` | the contract reference for the pooling clause; the form refuses a ticked consent without one |
| `patient_notice_version` | which patient-notice wording this lab adopted |

Returns `201` with `LabSummary`: `id`, `name`, `slug`, `status`,
`training_pooling_consent`. The form redirects to the new lab's page.

> `training_pooling_consent` is captured from the signed contract **at
> registration**. It is free to include before the first contract and
> near-impossible to retrofit across signed labs. When `true`, it is routed
> through the consent event log rather than set directly, so the append-only
> history is complete from the first moment.

[`register_lab`](radreport/onboarding/registration.py#L54) does six things in
one transaction: the `tenant` row, the first `app_user` (role `lab_admin`), a
`tenant_branding` row, a per-lab acceptance `eval_set` (~40 items), an opening
roster `import_batch`, and the consent event. It creates the tenant as
`provisioning` and transitions it to `onboarding` before returning — so
`provisioning` is never a state a client observes.

**The role is `lab_admin`, not `admin`.** "admin" is reserved for the platform
realm; a lab admin must never be escalatable to product admin by editing a
roles array.

The lab page shows the lab's status with a form to move it, its pipeline steps
and what serves each ([2.7](#27-configure-the-engines)), and links to its
onboarding and readiness pages.

## 1.2 Read a lab back

| Method | Path | Who | Returns | Source |
|---|---|---|---|---|
| GET | `/admin/api/labs` | `product_admin`, `support` | `LabSummary[]`, by name, every status | [`admin_api.py:62`](radreport/api/routes/admin_api.py#L62) |

This is the only JSON read of tenant state. **There is no
`GET /admin/api/labs/{tenant_id}`** — see [1.6](#16-what-has-no-crud-endpoint).

The lab list page hides offboarded labs; `GET /admin/labs?show=all` shows them.

## 1.3 The lifecycle status machine

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/admin/labs/{tenant_id}/status` | `product_admin` — the form on the lab page | [`admin_panel.py:349`](radreport/api/routes/admin_panel.py#L349) |
| POST | `/admin/api/labs/{tenant_id}/status` | `product_admin` | [`admin_api.py:107`](radreport/api/routes/admin_api.py#L107) |

Body `{"status": "<target>"}` (form field `status`); the API returns the updated
`LabSummary`. Transitions are whitelisted in
[`_ALLOWED_TRANSITIONS`](radreport/core/tenancy.py#L162) and enforced by
[`assert_transition_allowed`](radreport/core/tenancy.py#L177):

| From | May move to |
|---|---|
| `provisioning` | `onboarding`, `offboarded` |
| `onboarding` | `pilot` ⚠, `offboarded` |
| `pilot` | `live`, `suspended`, `offboarded` |
| `live` | `suspended`, `offboarded` |
| `suspended` | `live`, `pilot`, `offboarded` |
| `offboarded` | **nothing — terminal** |

Anything else is `409` with the legal targets in the message. An unknown
current status is also `409`. The lab page's form only offers the legal
targets.

⚠ `onboarding → pilot` is gated on readiness and belongs to the **end of Phase
2**, not here — see [2.8](#28-readiness-and-the-gate-to-pilot).

`suspended` is not an outage. It routes incoming reports to the manual fallback
path rather than failing them, and the transition logs a warning saying so.

### Driving this without a detail read

```
GET  /admin/api/labs                          → find the lab, read `status`
POST /admin/api/labs/{id}/status  {"status": "..."}
                                              → returns the updated LabSummary
```

The `POST` response is a `LabSummary`, so a client that only ever drives
transitions can track status from the write alone after an initial list.

## 1.4 Add the people

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/admin/labs/{tenant_id}/onboarding/roster` | `product_admin` — upload form on the onboarding page | [`admin_panel.py:590`](radreport/api/routes/admin_panel.py#L590) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/roster` | `product_admin` | [`admin_api.py:160`](radreport/api/routes/admin_api.py#L160) |

`multipart/form-data`: `file` (an HR CSV export) and, on the API, `trigger`
(`initial_onboarding` by default; `new_radiologist`, `site_expansion`, …). Up to
50 MiB.
