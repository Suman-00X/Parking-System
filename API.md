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

Every route, who may call it and exactly which parameters it accepts are declared in one file:
[`api/access_policy.xml`](radreport/api/access_policy.xml). It is read once at
startup by [`load_policy`](radreport/api/access.py#L191), and
[`AccessMiddleware`](radreport/api/access.py#L297) checks every request against
it **before any route handler runs**.

Each `<route>` names a method, a path, a realm, the roles allowed to call it
(`roles=`, comma-separated), a rate limit, for anything that takes a body a
maximum body size, and one `<param>` per accepted parameter:

```xml
<route id="admin.lab.status" method="POST" path="/admin/labs/{tenant_id}/status"
       roles="product_admin" rate-limit="admin-write" max-body-bytes="4096">
  <param name="tenant_id" in="path" type="uuid" required="true"/>
  <param name="status" in="form" type="string" required="true"
         pattern="provisioning|onboarding|pilot|live|suspended|offboarded"/>
</route>
```

A `<param>` has `in=` `path` | `query` | `form` | `file` | `json` (a top-level
key of a JSON object body), `type=` `string` | `uuid` | `int` | `number` |
`bool` | `object` | `array` | `file`, `required="true"` when mandatory,
`pattern=` (must match the whole value), `max-length=` (characters for a string,
items for an array, files for a file field; a string without one gets 2000) and
`multiple="true"` for a repeatable query key or a list of files.

The middleware, in order:

| Step | Refusal |
|---|---|
| Match the method and path to a `<route>`. Unlisted, or limited by `environments` to other deployments (the docs routes) | `404 Not Found` |
| Compare the declared `Content-Length` with `max-body-bytes` (a fast early refusal) | `413` |
| Count the request against an IP-keyed rate limit | `429` with `Retry-After` |
| Identify the caller for the route's realm (below) | `401`, or a `303` to `/admin/login` |
| Count it against a caller-keyed rate limit | `429` with `Retry-After` |
| Check the caller's role is one the route allows | `403` |

Then [`InputValidationMiddleware`](radreport/api/input_check.py) checks the
parameters, still before any handler runs, and only for a caller already let
through, so nothing is read for a stranger:

| Step | Refusal |
|---|---|
| Path and query: every key declared, none repeated unless `multiple`, each value of its type and pattern, every required one present | `400` |
| Read the body into a spool (memory up to 1 MiB, then a temporary file), counting bytes as they arrive, so a chunked body without a `Content-Length` is capped too. A route without `max-body-bytes` may carry 64 KiB | `413` |
| A route with no body parameters refuses any body; a JSON route needs `application/json` and an object; a form route needs urlencoded or multipart | `400` |
| Every JSON key, form field and file field declared, of its type, pattern and length, with no file count over `max-length`, nothing required missing | `400` |

The `400` names the parameter but never repeats its value, and a parameter
name that is not plain text is not repeated either. The body the handler then
reads is the spooled copy, byte for byte. This matters most for JSON: Pydantic
silently drops unknown keys, so without the policy a smuggled `"status": "live"`
on registration would be ignored rather than refused.

The identified caller is left on `request.state.identity`; handlers read it
through [`current_admin`](radreport/api/deps.py#L39) and
[`current_principal`](radreport/api/deps.py#L50).

**The app refuses to start if the policy and the routes disagree.**
[`verify_coverage`](radreport/api/access.py) fails `create_app()` when a
served route is not in the file, or the file lists a route that is not served;
[`verify_params`](radreport/api/input_check.py) fails it when a handler accepts
a parameter its route does not declare, or the reverse, or when `required` or
`type` differ. A new route or parameter without a policy entry cannot ship by
accident.

**Adding a role or a permission is an edit to the XML**, not to code: declare
the `<role>` under its realm, and add it to the `roles=` of each route it
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
`/review` request is ignored, and a lab token on an `/admin` request is
ignored.

### Lab users — `app_user`, by bearer token

Admins and lab users get different mechanisms on purpose. Admin accounts are few,
browser-only and the most powerful, so they get server-side sessions that end the
instant they are revoked. Lab traffic includes dictation devices and hospital
integrations as well as browsers, and grows with every lab, so it gets short-lived
signed tokens that the middleware checks without a database round trip.

```
POST /auth/login     {"lab": "<lab slug>", "email": "...", "password": "..."}
  -> {"access_token": "...", "token_type": "bearer", "expires_in": 900, "refresh_token": "..."}

Authorization: Bearer <access_token>      on every lab-realm request
```

- **Access token:** HS256-signed, 15 minutes, carrying the user id, tenant id and
  roles. [`_identify_lab_user`](radreport/api/access.py) verifies signature,
  expiry, issuer and type; a missing, forged or expired token is `401` with
  `WWW-Authenticate: Bearer`. The roles in the token are checked against the
  route's `roles=` — `403` if none match. Handlers keep their own finer checks.
- **Refresh token:** `POST /auth/refresh {"refresh_token": ...}` returns a new
  pair. It lasts 14 days, is stored only as a SHA-256 hash in the per-lab,
  RLS-isolated `lab_refresh_token` table, and is single-use: each refresh retires
  the old one. Presenting a retired token again is treated as theft and revokes
  every token from that sign-in.
- **Sign-out:** `POST /auth/logout {"refresh_token": ...}` ends that sign-in.
- **Passwords:** a product admin sets them from the lab page or
  `POST /admin/api/labs/{tenant_id}/users/{user_id}/password`; a user changes
  their own at `POST /auth/password`. Either signs the user out everywhere.
- **Revocation window:** deactivating a user or changing their password stops new
  access tokens at once; an access token already issued keeps working until it
  expires, at most 15 minutes. That is the price of not hitting the database on
  every request.
- **Every failed sign-in answers the same `401 invalid lab, email or password`**,
  whether the lab, the email or the password was wrong, and takes the same time.
- The signing secret is `RADREPORT_LAB_AUTH__TOKEN_SECRET`. Outside
  local/test/development the app refuses to start without one of at least 32
  characters.

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
| Lab user | the tenant id inside their signed access token | [`get_db`](radreport/api/deps.py#L61) |
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

**This is the only way to add a user — radiologist, transcriptionist, lab admin
or auditor alike.** There is no per-user endpoint; everyone arrives through
this CSV, uploaded by a product admin for the lab.

CSV columns ([`parse_roster_csv`](radreport/onboarding/roster.py#L57)):

| Column | Required | Notes |
|---|---|---|
| `employee_code` | ✅ | the identity key; duplicates **within one file** are rejected as a data-entry error |
| `display_name` | ✅ | — |
| `email` | — | — |
| `roles` | — | `;`-separated, validated against `UserRole`; **empty defaults to `radiologist`** |
| `subspecialty` | — | `;`-separated |
| `default_language` | — | defaults to `en-IN` |

So a transcriptionist is a row with `roles=transcriptionist`, and a
radiologist who is also a lab admin is `roles=radiologist;lab_admin`.

Returns `batch_id`, `created`, `updated`, `profiles_created`, `problems[]`. A
file that parses to **zero** rows with problems is `422`; partial problems come
back in `problems[]` alongside a successful import.

**Re-import is an upsert, and roles are additive.**
[`import_roster`](radreport/onboarding/roster.py#L106) matches on
`employee_code`, refreshes `display_name`/`email`, sets `is_active = True`, and
**unions** the roles — a re-import must not silently strip a role an admin
granted after the first import. A `radiologist` row also gets a
`radiologist_profile` created on first sight, carrying language and
subspecialty.

The batch records no submitting lab user; who uploaded it is in the
`admin_org_selected` audit row the request wrote.

## 1.5 Voice and the two consents

Lab side: these consents belong to the radiologist, not the vendor.

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/onboarding/radiologists/{radiologist_id}/voice-enrollment` | `lab_admin`, `radiologist` | [`onboarding.py:69`](radreport/api/routes/onboarding.py#L69) |
| POST | `/onboarding/radiologists/{radiologist_id}/training-consent` | `lab_admin`, `radiologist` | [`onboarding.py:88`](radreport/api/routes/onboarding.py#L88) |

Enrollment body: `embedding` (**exactly 192 floats**) and `consent_ref`
(non-empty). No voiceprint without a signed consent reference — a missing one
is `422` ([`enroll_voice`](radreport/onboarding/roster.py#L150)).

Training-consent body: `{"consent_ref": "..."}` or `{"consent_ref": null}`.
Returns `{"granted": bool}`.

**These are two different consents and two different rows.** Enrollment consent
permits a voiceprint for speaker identification. Training consent permits the
radiologist's audio to pool into model training. A `null` `consent_ref` on the
second records a **refusal** — a real answer, not a missing one: the
radiologist stays enrolled and their audio never pools
([`record_training_consent`](radreport/onboarding/roster.py#L169)).

## 1.6 What has no CRUD endpoint

Worth knowing before you go looking.

| Thing | Status |
|---|---|
| Read one lab as JSON | **No `GET /admin/api/labs/{id}`.** Filter the list, read the `POST .../status` response, or open the `/admin/labs/{id}` page |
| Legal next statuses | Not in any JSON response. The lab page offers only legal targets; an API client hard-codes the table in [1.3](#13-the-lifecycle-status-machine) or discovers the boundary by eating a `409` |
| Create / update / delete one lab user | **None.** The roster CSV is the only write path, and it never deletes — re-import sets `is_active = True` and unions roles. (Platform users do have these — see [Platform users](#platform-users).) |
| Branding | **None.** `register_lab` inserts an empty `tenant_branding` row and nothing ever touches it again |
| Patients and studies | **None.** Nothing in `radreport/` constructs a `Patient` or a `Study` outside tests — see [Phase 3](#31-capture) |
| Offboarding | `offboard()` exists in the module but no route calls it. Reach `offboarded` through `POST .../status` |

---

# Phase 2 — Teach the lab its knowledge

**34 routes.** The longest phase, and the one the product is really about. The
lab's own templates, vocabulary, normals and safety rules are extracted from
its historical material, each behind a human gate.

The work is split by realm. **A product admin uploads the lab's material and
runs the mining steps**, from the lab's onboarding page or the admin API.
**The lab's own staff make every clinical decision** on the `/onboarding`
routes; gates marked **R** are radiologist-only, because they are clinical calls
rather than data-quality ones.

> **The shape repeats at every stage: machine proposes, human disposes.** A
> mining or seeding step writes *candidates*. A separate **R** route accepts
> them. Nothing a machine produced goes live without a named radiologist on the
> row.

## 2.0 The step runner

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/admin/labs/{tenant_id}/onboarding/steps/{step}` | `product_admin` — a button per step on the onboarding page | [`admin_panel.py:622`](radreport/api/routes/admin_panel.py#L622) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/steps/{step}` | `product_admin` | [`admin_api.py:209`](radreport/api/routes/admin_api.py#L209) |

The parameterless mining and seeding steps share one route. `{step}` is a name
from [`STEPS`](radreport/admin/onboarding_steps.py#L110):

| `{step}` | Does | Section |
|---|---|---|
| `derive-map` | Derive the report-to-template map | [2.2](#22-feed-the-historical-reports-s2) |
| `lexicon-mine` | Mine terms from the corpus | [2.3](#23-mine-the-vocabulary-s3) |
| `collision-audit` | Run the sound-alike collision audit | [2.3](#23-mine-the-vocabulary-s3) |
| `mine-variants` | Mine what the ASR actually heard | [2.4](#24-verbatim-annotation-s4) |
| `boilerplate-mine` | Rank normal statements | [2.5](#25-rank-the-normals-s5) |
| `critical-rules-seed` | Propose critical-finding rules | [2.6](#26-author-the-safety-rules-s6) |
| `acceptance-assemble` | Fill the lab's acceptance set from its verbatim transcripts | [2.8](#28-readiness-and-the-gate-to-pilot) |
| `acceptance-freeze` | Freeze the acceptance set | [2.8](#28-readiness-and-the-gate-to-pilot) |

An unknown name is `404` listing the valid ones. The API takes an optional JSON
body for the two steps that have options — `{"min_frequency": n}` for
`lexicon-mine`, `{"verified_only": true}` for `boilerplate-mine` — and returns
the step's result. The page's buttons run with the defaults and redirect with a
one-line summary of the counts.

## 2.1 Templates (S1)

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/admin/labs/{tenant_id}/onboarding/templates` | `product_admin` — upload form | [`admin_panel.py:601`](radreport/api/routes/admin_panel.py#L601) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/templates` | `product_admin` | [`admin_api.py:169`](radreport/api/routes/admin_api.py#L169) |
| GET | `/onboarding/templates/candidates?batch_id=` | `lab_admin`, `radiologist` | [`onboarding.py:112`](radreport/api/routes/onboarding.py#L112) |
| POST | `/onboarding/templates/candidates/{candidate_id}/review` | **R** | [`onboarding.py:129`](radreport/api/routes/onboarding.py#L129) |
| POST | `/admin/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | `product_admin` — link per template batch | [`admin_panel.py:612`](radreport/api/routes/admin_panel.py#L612) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | `product_admin` | [`admin_api.py:200`](radreport/api/routes/admin_api.py#L200) |
| POST | `/onboarding/merge-proposals/{proposal_id}/decide` | **R** | [`onboarding.py:145`](radreport/api/routes/onboarding.py#L145) |
| POST | `/onboarding/batches/{batch_id}/apply` | **R** | [`onboarding.py:157`](radreport/api/routes/onboarding.py#L157) |
| POST | `/onboarding/batches/{batch_id}/revert` | **R** | [`onboarding.py:175`](radreport/api/routes/onboarding.py#L175) |

Submission takes `files` (multipart, several documents, up to 50 MiB in all; on
the API also an optional `trigger`) and returns `batch_id`, `candidates`,
`low_confidence`, `failures[]` (`filename`, `reason`). No files is `422`.

The candidate list comes back **lowest parse confidence first** — the work
queue is ordered by how likely the machine is to be wrong. Each entry carries
`parse_confidence`, `field_count`, `needs_field_by_field_review` and
`merged_into_template_id`.

Review body: `decision` (`approved` \| `edited` \| `rejected` \| `merged`,
default `approved`) plus optional overrides `spoken_study_code`, `code`,
`modality`, `body_region`, `json_schema`.

**Nothing goes live until `apply`**, which promotes approved candidates into
`template_version` and is where the refusals land: `409` if the batch has
blocking issues or is in the wrong state, `412` if approvals are outstanding.
`revert` re-points `is_current` at the previous version.

Merge proposals are near-duplicate detection, run **before** routing is
trained — two templates that are really one would otherwise teach the router a
distinction that does not exist. The admin route returns `{"proposals": n}`; a
batch that is not this lab's is `404`.

## 2.2 Feed the historical reports (S2)

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/admin/api/labs/{tenant_id}/onboarding/corpus` | `product_admin` | [`admin_api.py:194`](radreport/api/routes/admin_api.py#L194) |
| POST | `.../onboarding/steps/derive-map` | `product_admin` — see [2.0](#20-the-step-runner) | — |
| POST | `/onboarding/corpus/mappings/{mapping_id}/verify` | **R** | [`onboarding.py:190`](radreport/api/routes/onboarding.py#L190) |
| GET | `/onboarding/corpus/histogram?verified_only=` | `lab_admin`, `radiologist` | [`onboarding.py:203`](radreport/api/routes/onboarding.py#L203) |
| GET | `/onboarding/corpus/referrer-prior` | `lab_admin`, `radiologist` | [`onboarding.py:210`](radreport/api/routes/onboarding.py#L210) |

This is the report feeding step, and everything downstream depends on it.

The corpus load is **API only** — it arrives as structured records, so the
onboarding page points at the API rather than offering a form. It takes
`{"records": [...], "trigger": "..."}`, up to 100 MiB. Each record:
`report_text` (required) plus `external_report_id`,
`radiologist_employee_code`, `referring_doctor`, `patient_sex`,
`patient_age_years`, `is_deidentified`. Returns `batch_id`, `loaded`,
`duplicates`, `rejected[]` (`id`, `reason`).

`derive-map` guesses which template each historical report was written from and
returns `mapped`, `unmapped`, `by_method`, `verified`, `verification_target`.
**It is a guess.** `verification_target` is how many a radiologist has to
confirm before it is trusted; poll it from the `verify` response rather than
recomputing it.

The two reads are what the corpus is *for*:

- **`histogram`** — power-law head detection. `share` and `cumulative_share`
  per template, which is how the ~20 templates V1 actually ships get chosen.
- **`referrer-prior`** — `P(template | referring doctor)`, fed to the router as
  a prior.

## 2.3 Mine the vocabulary (S3)

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `.../onboarding/steps/lexicon-mine` | `product_admin` — see [2.0](#20-the-step-runner) | — |
| POST | `.../onboarding/steps/collision-audit` | `product_admin` — see [2.0](#20-the-step-runner) | — |
| POST | `/onboarding/collision-findings/{finding_id}/resolve` | **R** | [`onboarding.py:222`](radreport/api/routes/onboarding.py#L222) |

Mining returns `batch_id`, `pass_number`, `terms_extracted`, `terms_new`,
`terms_pending_review`, `ambiguous`. It is re-runnable — pass 2 follows
verbatim annotation ([2.4](#24-verbatim-annotation-s4)).

**The collision audit is blocking.** It finds near-homophone term pairs — the
canonical example being LMC against LMP — and returns `new_findings`,
`blocking`, and `findings[]` with `id`, `label_a`, `label_b`, `distance`,
`collision_class`, `severity`. Every finding with `severity: "block"` must be
resolved by a radiologist before the lab can proceed.

## 2.4 Verbatim annotation (S4)

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/onboarding/verbatim/queue?limit=` | `transcriptionist`, `lab_admin`, `radiologist` | [`onboarding.py:235`](radreport/api/routes/onboarding.py#L235) |
| POST | `/onboarding/verbatim` | **`transcriptionist`** | [`onboarding.py:254`](radreport/api/routes/onboarding.py#L254) |
| POST | `.../onboarding/steps/mine-variants` | `product_admin` — see [2.0](#20-the-step-runner) | — |

The critical path, served as a work queue: `outstanding`, `current[]`,
`legacy[]`, `gold_progress`, `corpus_hours`. `current` before `legacy` —
audio from the device class actually in use is worth more than archive audio.

Submission body: `recording_id`, `text`, `includes_disfluencies`,
`is_eval_set_member`.

**`includes_disfluencies: false` records the transcript but makes it
permanently ineligible for ASR training** — a cleaned transcript teaches a
model to delete words. Eval-set members are excluded for the obvious reason.
`training_eligible` in the response is the conjunction of both.

`mine-variants` closes the loop with [2.3](#23-mine-the-vocabulary-s3): it
scans paired audio for what the ASR *actually heard* for each known term, and
returns `transcripts_scanned`, `variants_written`, `terms_touched`,
`unmatched_frequent[]`.

## 2.5 Rank the normals (S5)

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `.../onboarding/steps/boilerplate-mine` | `product_admin` — see [2.0](#20-the-step-runner) | — |
| GET | `/onboarding/boilerplate/export` | `lab_admin`, `radiologist` | [`onboarding.py:267`](radreport/api/routes/onboarding.py#L267) |
| POST | `/onboarding/boilerplate/{candidate_id}/promote` | **R** | [`onboarding.py:279`](radreport/api/routes/onboarding.py#L279) |

Mining ranks the normal statements this lab writes, by corpus share, and
returns `candidates_written`, `fields_scanned`, `per_template`. The V1
deliverable is a CSV (`{"csv": "..."}`), not a screen.

Promotion and auto-fill are **two decisions**: `enable_auto_fill` defaults to
`false`, and a critical field refuses auto-fill outright (`422`). Returns
`template_field_id`, `absence_policy`, `default_normal_text`.

## 2.6 Author the safety rules (S6)

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `.../onboarding/steps/critical-rules-seed` | `product_admin` — see [2.0](#20-the-step-runner) | — |
| POST | `/onboarding/critical-rules` | **R** | [`onboarding.py:304`](radreport/api/routes/onboarding.py#L304) |
| POST | `/onboarding/critical-rules/{rule_id}/approve` | **R** | [`onboarding.py:316`](radreport/api/routes/onboarding.py#L316) |

Seeding proposes rules from the corpus and returns `created[]`, `candidates[]`
(with `code`, `finding_label`, `severity`, `corpus_mentions`, `examples`) and
`lab_specific_phrases[]`. **Every seeded rule is inactive and unapproved.**

Authoring body: `code`, `finding_label`, `pattern`, `severity`, `sla_minutes`
(> 0), `escalation_path` (**≥ 1 entry**), `pattern_type` (`lexical`),
`negation_sensitive` (`true`), `requires_ack` (`true`).

**A rule with no escalation path is refused.** An alert nobody is obliged to
receive is not a safety control. The label, SLA and escalation path are
clinical, which is why authoring is **R** and not an admin step.

## 2.7 Configure the engines

Interleaved with the stages above, not after them: a product admin picks which
model serves each pipeline step while the lab is still onboarding.

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/admin/providers` | `product_admin`, `support` — providers and models page | [`admin_panel.py:425`](radreport/api/routes/admin_panel.py#L425) |
| POST | `/admin/providers` | `product_admin` | [`admin_panel.py:502`](radreport/api/routes/admin_panel.py#L502) |
| POST | `/admin/models` | `product_admin` | [`admin_panel.py:512`](radreport/api/routes/admin_panel.py#L512) |
| POST | `/admin/labs/{tenant_id}/assign` | `product_admin` — the *Propose* form on the lab page | [`admin_panel.py:404`](radreport/api/routes/admin_panel.py#L404) |
| POST | `/admin/labs/{tenant_id}/assignments/{assignment_id}/activate` | `product_admin` — the *activate* button next to a proposal | [`admin_panel.py:414`](radreport/api/routes/admin_panel.py#L414) |
| GET | `/admin/api/labs/{tenant_id}/steps` | `product_admin`, `support` | [`admin_api.py:120`](radreport/api/routes/admin_api.py#L120) |
| POST | `/admin/api/labs/{tenant_id}/assignments` | `product_admin` | [`admin_api.py:131`](radreport/api/routes/admin_api.py#L131) |
| POST | `/admin/api/labs/{tenant_id}/assignments/{assignment_id}/activate` | `product_admin` | [`admin_api.py:141`](radreport/api/routes/admin_api.py#L141) |

Form fields: providers take `name`, `kind` (`cloud_api` \|
`local_openai_compatible`), `api_key_env_var`, `default_endpoint`; models take
`provider_id`, `model_identifier`, `display_name`, `endpoint_override`;
`assign` takes `task_key` and `model_definition_id`. The API's propose body is
the same pair as JSON, and returns `201` with `assignment_id` and `status`.

**Cloud API keys are never entered or stored here.** A provider names an
*environment variable*; the server reads the key from its own environment. A
locally hosted model is configured by address instead.

`GET /admin/api/labs/{id}/steps` is the same per-step view as the lab page, for
scripting an onboarding: `task_key`, `task_bucket`, `is_asr`, `active_model`,
`active_provider`, `is_local`, `is_configured`, `proposed[]` (`assignment_id`,
`label`).

**Proposing does not make a model live.** Activation is a separate call, and it
is gated: it is `412` if the assignment has no gold-set eval run behind it, and
`404` if it does not resolve for this lab. That gate is what the engine bake-off
exists to satisfy. On the page, a refusal comes back as the lab page with the
reason shown.

A step with no active model does not degrade — the pipeline fails at the first
such step. The lab page counts them and says so.

## 2.8 Readiness and the gate to pilot

**The acceptance set comes first.** The `gold_set_frozen` check needs a frozen
per-lab acceptance set of at least 40 items. Registration creates it empty; fill
it once the transcriptionists have produced verbatim transcripts (2.4):

```
POST /admin/api/labs/{id}/onboarding/steps/acceptance-assemble   → {"items", "target": 40, "short_by"}
POST /admin/api/labs/{id}/onboarding/steps/acceptance-freeze     → 409 while short of 40
```

Only current-hardware recordings with disfluency-preserving transcripts are
used, spread across radiologists and audio quality; chosen transcripts are
excluded from training for good. Assembling again adds to the set until it is
frozen; after that both steps answer `409`, because readiness was measured
against it.

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/admin/labs/{tenant_id}/onboarding` | `product_admin`, `support` — the onboarding page | [`admin_panel.py:523`](radreport/api/routes/admin_panel.py#L523) |
| GET | `/admin/api/labs/{tenant_id}/onboarding` | `product_admin`, `support` | [`admin_api.py:154`](radreport/api/routes/admin_api.py#L154) |
| GET | `/admin/labs/{tenant_id}/readiness` | `product_admin`, `support` — **HTML** | [`admin_panel.py:359`](radreport/api/routes/admin_panel.py#L359) |
| GET | `/admin/api/labs/{tenant_id}/readiness` | `product_admin`, `support` | [`admin_api.py:96`](radreport/api/routes/admin_api.py#L96) |

The onboarding status is the view across every stage
([`onboarding_overview`](radreport/admin/onboarding_steps.py#L33)):
`corpus_verification` (`verified`, `target`), `gold_progress`,
`active_critical_rules`, `recent_batches[]` (last 20), and `readiness` with
`passed`, `failures[]`, `warnings[]` and the full `checks[]`. The onboarding
page renders the same, with the upload forms, a button per
[step](#20-the-step-runner) and a *propose merges* link on each template batch.

`GET /admin/api/labs/{id}/readiness` is the gate alone:
`{"passed": bool, "checks": [{check_id, status, measured_value, threshold,
detail}]}`. The readiness page is the same report as a screen, blocking checks
first.

All four evaluate with `persist=False`
([`evaluate_readiness`](radreport/onboarding/readiness.py#L148)), so none of
them writes a `readiness_check` row and any of them is a safe dry run. Each
opens the lab's session through `admin_lab_session`, so RLS sees the lab's rows.

### Then, and only then

```
GET  /admin/api/labs/{id}/readiness           → confirm `passed: true`
POST /admin/api/labs/{id}/status  {"status": "pilot"}
```

— or, in the browser, the readiness page and then the status form on the lab
page.

`onboarding → pilot` is **the one gated transition**
([`S7_GATED_TRANSITION`](radreport/core/tenancy.py#L165)).
[`transition_status`](radreport/onboarding/registration.py#L111) re-runs
readiness itself and refuses on any fail-severity check:
`409 onboarding -> pilot is gated on readiness: every fail-severity check must
pass first`. Checking first only tells you whether the write will succeed; it
does not make it succeed.

---

# Phase 3 — Runtime: one report, end to end

**12 routes.** A dictation arrives and leaves as a signed report. This is the
product. Every route here is in the lab realm.

## 3.1 Capture

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/ingest/recordings` | `radiologist`, `lab_admin` | [`ingest.py:38`](radreport/api/routes/ingest.py#L38) |

`multipart/form-data`, up to 512 MiB:

| Field | Type | Default |
|---|---|---|
| `file` | file | required |
| `study_id` | uuid | required |
| `radiologist_id` | uuid | required |
| `device_id` | string | `null` |
| `capture_device_class` | `legacy` \| `dictation_mic_ptt` \| `headset` | `dictation_mic_ptt` |
| `is_push_to_talk` | bool | `true` |

`201` returns `recording_id`, `content_hash`, `duration_seconds`,
`sample_rate_hz`, `audio_format`, `measured_snr_db`, `silence_ratio`,
`capture_device_class`, `warnings[]`.

- `409` — already ingested. `detail` is an **object**:
  `{"message": ..., "recording_id": ..., "content_hash": ...}`. Idempotent by
  content hash, so treat this as success and take the id.
- `422` — a fail-severity audio gate. `detail` is an object:
  `{"message": ..., "code": ...}`. Warn-severity gates come back in `warnings`
  on the `201` instead.

**`study_id` has to come from somewhere, and nothing here creates it.** No
route constructs a `Study` or a `Patient`; outside tests, neither type is
instantiated anywhere in `radreport/`. A real deployment needs an
ADT/ORM feed that does not exist yet.

## 3.2 The missing link

**Capture does not start the pipeline.** `/ingest/recordings` validates the
audio, stores the object, writes the row, records the audit entry, and returns.
No run is enqueued.

Nothing under `api/` imports `pipeline/`. The only callers of
`build_v1_graph()` and `new_run()` are in `tests/db/test_pipeline_v1.py`, and
`workers/` is empty. The largest module in the repository — the product
itself — is unreachable over HTTP.

Everything below assumes a `report_draft` already exists. To see how one is
made, read that test file, not a request trace.

## 3.3 Take work from the queue

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/review/queue?limit=` | `radiologist`, `transcriptionist`, `lab_admin`, `auditor` | [`review.py:68`](radreport/api/routes/review.py#L68) |
| GET | `/review/queue/stats` | `radiologist`, `transcriptionist`, `lab_admin`, `auditor` | [`review.py:79`](radreport/api/routes/review.py#L79) |

Ordered priority → critical alert → flagged-field count → oldest, and
**filtered to what the caller's role may actually complete**. A transcriptionist
does not see drafts only a radiologist can finish.

Each item: `draft_id`, `template_code`, `template_display_name`, `priority`,
`confidence`, `flagged_field_count`, `status`, `has_critical_alert`,
`requires_radiologist`, `waiting_minutes`.

`stats` is for the header and carries no clinical content: `total`,
`awaiting_radiologist`, `awaiting_assistant`, `with_critical_alert`,
`oldest_waiting_minutes`, `median_flagged_fields`.

## 3.4 Open a draft

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/review/drafts/{draft_id}` | `radiologist`, `transcriptionist`, `auditor` | [`review.py:91`](radreport/api/routes/review.py#L91) |
| GET | `/review/drafts/{draft_id}/audio` | `radiologist`, `transcriptionist`, `auditor` | [`review.py:137`](radreport/api/routes/review.py#L137) |

Returns `draft_id`, `status`, `rendered_text`, `overall_confidence`,
`fields[]`, `findings`, and a `signing` preflight:

```json
{
  "signing": {
    "may_sign": false,
    "blocking_findings": [],
    "unacknowledged_alerts": ["..."],
    "ungrounded_fields": []
  }
}
```

Render `may_sign` from this rather than inferring it — the same four refusals
are re-checked server-side at sign time and come back as `409`.

Each field carries `fill_source` (`dictated` \| `template_default` \|
`blanket_normal` \| `inferred` \| `human`), `system_asserted`, `is_grounded`,
`is_flagged`, `is_critical`, `confidence`, `flag_reasons[]` and `provenance[]`.
**`system_asserted` is the most important visual distinction on the screen**: it
marks a value no human said out loud.

Audio responses carry `Cache-Control: private, no-store` — it is PHI and must
not be cached by an intermediary. `503` if the object store is unreachable.

## 3.5 Save edits

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/review/drafts/{draft_id}/revisions` | `radiologist`, `transcriptionist` | [`review.py:185`](radreport/api/routes/review.py#L185) |

Body: `edits[]` (`field_value_id` plus any of `value_text`, `value_numeric`,
`value_unit`, `value_enum`, `assertion_status`, `laterality`),
`rendered_text`, `active_edit_seconds`, `wall_clock_seconds`.

`active_edit_seconds` is **focus time**, measured by the review screen's
blur/focus timer — not wall clock. The server clamps implausible values; check
`clamped` in the response.

Returns `revision_id`, `revision_number`, `active_edit_seconds`, `clamped`, and
`edits[]` with a per-field `edit_type`, `error_category` and `has_audio_span`.
Those categorised edit events are the training signal.

## 3.6 Sign

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/review/drafts/{draft_id}/sign` | **`radiologist`** | [`review.py:198`](radreport/api/routes/review.py#L198) |

No body. Returns `final_report_id`, `content_hash`, `path_type`, `signed_at`.

`path_type` records how the report got here: `transcriptionist_reviewed`,
`radiologist_only`, or `autonomous` (released with no human review, under a
granted autonomy class — see [4.5](#45-autonomy)).

Refused with `409` on any outstanding gate. The preflight in
[3.4](#34-open-a-draft) tells you which.

## 3.7 After signing

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/review/reports/{report_id}/addendum` | **`radiologist`** | [`review.py:214`](radreport/api/routes/review.py#L214) |
| POST | `/review/alerts/{alert_id}/acknowledge` | **`radiologist`** | [`review.py:230`](radreport/api/routes/review.py#L230) |

Addendum body: `rendered_text` and a non-empty `reason`. **The signed original
is never modified** — an addendum is a new row that references it.

Acknowledgement body: `{"outcome": "true_positive" | "false_positive"}`.
Returns `is_breach`, which is whether the rule's SLA was missed. Unacknowledged
alerts are one of the four things that block signing.

## 3.8 The screens

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/ui/queue` | `radiologist`, `transcriptionist`, `lab_admin`, `auditor` | [`review_ui.py:187`](radreport/api/routes/review_ui.py#L187) |
| GET | `/ui/drafts/{draft_id}` | `radiologist`, `transcriptionist`, `auditor` | [`review_ui.py:99`](radreport/api/routes/review_ui.py#L99) |
| GET | `/ui/static/{name}` | public | [`review_ui.py:27`](radreport/api/routes/review_ui.py#L27) |

Server-rendered against the same functions as `/review`, no bundler and no
build step. `static` serves exactly `review.js` and `review.css` (the admin
panel uses the stylesheet too); anything else is `404`.

---

# Phase 4 — Operate it

**16 routes.** The lab is signing reports. Now: is the output good, can it
leave the building, and can any of the review be removed?

## 4.1 Grade the output

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/review/reports/{report_id}/grade` | **`radiologist`** | [`review.py:246`](radreport/api/routes/review.py#L246) |
| GET | `/review/metrics/cse-rate` | **`radiologist`** | [`review.py:257`](radreport/api/routes/review.py#L257) |

Body: `grade` and an optional `note`. Grades are `G0`–`G4`
([`SeverityGrade`](radreport/core/types.py#L264)); `G3` and `G4` are clinically
significant errors ([`CSE_GRADES`](radreport/core/types.py#L274)), and `is_cse`
says so. `previous_grade` comes back when a report is re-graded.

`cse-rate` returns `graded`, `cse_count`, `rate` and `is_reportable` — the
denominator travels with the number, because a rate over six reports is not a
rate.

**This is the evidence autonomy is granted on.** No grading, no grants.

## 4.2 Ask whether the draft helped

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/review/drafts/{draft_id}/usefulness` | `radiologist`, `transcriptionist` | [`review.py:273`](radreport/api/routes/review.py#L273) |
| GET | `/review/metrics/usefulness` | `radiologist`, `transcriptionist`, `lab_admin`, `auditor` | [`review.py:284`](radreport/api/routes/review.py#L284) |

Body: `was_useless` and an optional `reason`. The rollup returns `reported`,
`useless`, `rate`, `is_alarming`.

A one-click verdict that is actually recorded — the early warning for
reviewers disengaging, which a quality metric computed from edits would miss.

## 4.3 Send it to the hospital system

| Method | Path | Who | Returns | Source |
|---|---|---|---|---|
| GET | `/ga/export/{report_id}/hl7` | `radiologist`, `lab_admin` | `application/hl7-v2` | [`ga.py:102`](radreport/api/routes/ga.py#L102) |
| GET | `/ga/export/{report_id}/fhir` | `radiologist`, `lab_admin` | FHIR R4 `DiagnosticReport` | [`ga.py:114`](radreport/api/routes/ga.py#L114) |

HL7 v2 `ORU^R01`, MLLP-framed; FHIR R4 `DiagnosticReport` with a transaction
bundle. A report that amends another is marked as a correction in both.

**Export refuses rather than guesses.** A report with no accession number is
`412`, not a message with an empty field. FHIR references the patient
(`Patient/{pseudonym}`) instead of inlining demographics — identity stays out
of anything this system originates.

## 4.4 Watch for drift

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/ga/drift?baseline_days=60&window_days=14` | `lab_admin`, `radiologist`, `auditor` | [`ga.py:144`](radreport/api/routes/ga.py#L144) |

PSI of a recent window against an **explicit** baseline window, plus
`significant[]` naming the metrics that moved. Both windows are parameters, so
the comparison is never against "whatever happened to be there".

## 4.5 Autonomy

Two sides. The lab can watch and revoke; only a product admin can open accrual
and grant.

| Method | Path | Who | Source |
|---|---|---|---|
| GET | `/ga/autonomy/{class_code}` | `lab_admin`, `radiologist`, `auditor` | [`ga.py:45`](radreport/api/routes/ga.py#L45) |
| POST | `/ga/autonomy/{class_code}/revoke` | `radiologist`, `lab_admin` | [`ga.py:71`](radreport/api/routes/ga.py#L71) |
| GET | `/ga/autonomy-coverage?days=30` | `radiologist`, `lab_admin`, `auditor` | [`ga.py:82`](radreport/api/routes/ga.py#L82) |
| GET | `/admin/api/labs/{tenant_id}/autonomy/{class_code}` | `product_admin`, `support` | [`admin_api.py:219`](radreport/api/routes/admin_api.py#L219) |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/open-accrual` | **`product_admin`** | [`admin_api.py:229`](radreport/api/routes/admin_api.py#L229) |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/grant` | **`product_admin`** | [`admin_api.py:239`](radreport/api/routes/admin_api.py#L239) |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/revoke` | **`product_admin`** | [`admin_api.py:255`](radreport/api/routes/admin_api.py#L255) |

The sequence is **observe → decide → measure**, and each step is a separate
call on purpose.

The two reads are the same observation, from either side, and grant nothing:
`class_code`, `baseline_cse_rate`, `graded_n`, `required_n`, `observed_cse`,
`observed_rate`, `posterior_prob_ni`, `meets_volume`, `would_support_grant`.
That last field is named in the conditional deliberately — observing and
deciding are separate steps, and these endpoints are only the observation.

`open-accrual` starts collecting evidence (`409` if it is already open).
`grant` is the Bayesian sequential decision: `412` if the evidence does not
support it. Returns `granted_at` and the `cusum_threshold` that will police it
from then on.

**Granting and revoking are deliberately asymmetric.** Only a product admin may
grant — a lab must not grant itself autonomy over its own reports. Either side
may revoke at any time, with a mandatory non-empty `reason`.

`autonomy-coverage` closes the loop: `window_days`, `signed`, `reviewed`,
`released_without_review`, `share`, `target`, `meets_target`. A grant that
removes no review is not worth its risk.

## 4.6 Retrain the engines

| Method | Path | Who | Source |
|---|---|---|---|
| POST | `/admin/api/labs/{tenant_id}/adaptation/gates` | **`product_admin`** | [`admin_api.py:270`](radreport/api/routes/admin_api.py#L270) |
| POST | `/admin/api/labs/{tenant_id}/adaptation/require-gates` | **`product_admin`** | [`admin_api.py:279`](radreport/api/routes/admin_api.py#L279) |

Body: `{"recording_ids": [...], "target": "asr_global"}` — also `asr_speaker`,
`utterance_classifier`, `router`. Up to 1 MiB.

Six gates over a candidate training corpus. `gates` **reports** and names
`blocked_by[]`; `require-gates` is the raising form for a training entry point
and is `412`. Two of the six are unimplemented and **fail closed**, so
`require-gates` will currently refuse a corpus that should pass.

This is where [1.5](#15-voice-and-the-two-consents) and
[2.4](#24-verbatim-annotation-s4) are cashed in: training consent and
`includes_disfluencies` decide which recordings are even eligible to be named
here.

---

# Appendix A — Index by prefix

For lookup once you know where you are going. **Who** and **Limit** are copied
from [`access_policy.xml`](radreport/api/access_policy.xml), which wins if this
table disagrees. Body caps: KiB = 1,024 bytes, MiB = 1,048,576 bytes; routes
with no cap take no body.

Role shorthand: **PA** `product_admin`, **S** `support`, **R** `radiologist`,
**T** `transcriptionist`, **LA** `lab_admin`, **A** `auditor`.

### Public (10)

| Method | Path | Limit | Phase | Source |
|---|---|---|---|---|
| GET | `/health` | public | [0](#phase-0--is-the-service-up) | [`app.py:48`](radreport/api/app.py#L48) |
| GET | `/ready` | public | [0](#phase-0--is-the-service-up) | [`app.py:53`](radreport/api/app.py#L53) |
| GET | `/ui/static/{name}` | public | [3.8](#38-the-screens) | [`review_ui.py:27`](radreport/api/routes/review_ui.py#L27) |
| GET | `/openapi.json` | public · local/test/development only | [0](#phase-0--is-the-service-up) | FastAPI |
| GET | `/docs` | public · local/test/development only | [0](#phase-0--is-the-service-up) | FastAPI |
| GET | `/docs/oauth2-redirect` | public · local/test/development only | [0](#phase-0--is-the-service-up) | FastAPI |
| GET | `/redoc` | public · local/test/development only | [0](#phase-0--is-the-service-up) | FastAPI |
| GET | `/admin/login` | public | [I](#product-admins--platform_user-by-session-cookie) | [`admin_panel.py:143`](radreport/api/routes/admin_panel.py#L143) |
| POST | `/admin/login` | login · 4 KiB | [I](#product-admins--platform_user-by-session-cookie) | [`admin_panel.py:168`](radreport/api/routes/admin_panel.py#L168) |
| POST | `/admin/logout` | public · 4 KiB | [I](#product-admins--platform_user-by-session-cookie) | [`admin_panel.py:192`](radreport/api/routes/admin_panel.py#L192) |

### `/admin` — admin panel pages, HTML (21)

| Method | Path | Who | Limit | Phase | Source |
|---|---|---|---|---|---|
| GET | `/admin` | PA, S | admin-read | [I](#product-admins--platform_user-by-session-cookie) | [`admin_panel.py:202`](radreport/api/routes/admin_panel.py#L202) |
| GET | `/admin/labs` | PA, S | admin-read | [1.1](#11-register-the-lab) | [`admin_panel.py:207`](radreport/api/routes/admin_panel.py#L207) |
| POST | `/admin/labs` | PA | admin-write · 64 KiB | [1.1](#11-register-the-lab) | [`admin_panel.py:269`](radreport/api/routes/admin_panel.py#L269) |
| GET | `/admin/labs/{tenant_id}` | PA, S | admin-read | [1.1](#11-register-the-lab) | [`admin_panel.py:285`](radreport/api/routes/admin_panel.py#L285) |
| POST | `/admin/labs/{tenant_id}/status` | PA | admin-write · 4 KiB | [1.3](#13-the-lifecycle-status-machine) | [`admin_panel.py:349`](radreport/api/routes/admin_panel.py#L349) |
| GET | `/admin/labs/{tenant_id}/readiness` | PA, S | admin-read | [2.8](#28-readiness-and-the-gate-to-pilot) | [`admin_panel.py:359`](radreport/api/routes/admin_panel.py#L359) |
| POST | `/admin/labs/{tenant_id}/assign` | PA | admin-write · 4 KiB | [2.7](#27-configure-the-engines) | [`admin_panel.py:404`](radreport/api/routes/admin_panel.py#L404) |
| POST | `/admin/labs/{tenant_id}/assignments/{assignment_id}/activate` | PA | admin-write · 4 KiB | [2.7](#27-configure-the-engines) | [`admin_panel.py:414`](radreport/api/routes/admin_panel.py#L414) |
| GET | `/admin/providers` | PA, S | admin-read | [2.7](#27-configure-the-engines) | [`admin_panel.py:425`](radreport/api/routes/admin_panel.py#L425) |
| POST | `/admin/providers` | PA | admin-write · 4 KiB | [2.7](#27-configure-the-engines) | [`admin_panel.py:502`](radreport/api/routes/admin_panel.py#L502) |
| POST | `/admin/models` | PA | admin-write · 4 KiB | [2.7](#27-configure-the-engines) | [`admin_panel.py:512`](radreport/api/routes/admin_panel.py#L512) |
| GET | `/admin/labs/{tenant_id}/onboarding` | PA, S | admin-read | [2.8](#28-readiness-and-the-gate-to-pilot) | [`admin_panel.py:523`](radreport/api/routes/admin_panel.py#L523) |
| POST | `/admin/labs/{tenant_id}/onboarding/roster` | PA | admin-upload · 50 MiB | [1.4](#14-add-the-people) | [`admin_panel.py:590`](radreport/api/routes/admin_panel.py#L590) |
| POST | `/admin/labs/{tenant_id}/onboarding/templates` | PA | admin-upload · 50 MiB | [2.1](#21-templates-s1) | [`admin_panel.py:601`](radreport/api/routes/admin_panel.py#L601) |
| POST | `/admin/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | PA | admin-write · 4 KiB | [2.1](#21-templates-s1) | [`admin_panel.py:612`](radreport/api/routes/admin_panel.py#L612) |
| POST | `/admin/labs/{tenant_id}/onboarding/steps/{step}` | PA | admin-write · 4 KiB | [2.0](#20-the-step-runner) | [`admin_panel.py:622`](radreport/api/routes/admin_panel.py#L622) |
| GET | `/admin/users` | PA, S | admin-read | [I](#platform-users) | [`admin_panel.py:634`](radreport/api/routes/admin_panel.py#L634) |
| POST | `/admin/users` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_panel.py:689`](radreport/api/routes/admin_panel.py#L689) |
| POST | `/admin/users/{user_id}/deactivate` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_panel.py:710`](radreport/api/routes/admin_panel.py#L710) |
| POST | `/admin/users/{user_id}/reactivate` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_panel.py:715`](radreport/api/routes/admin_panel.py#L715) |
| POST | `/admin/users/{user_id}/password` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_panel.py:720`](radreport/api/routes/admin_panel.py#L720) |

### `/admin/api` — admin panel, JSON (24)

| Method | Path | Who | Limit | Phase | Source |
|---|---|---|---|---|---|
| GET | `/admin/api/labs` | PA, S | admin-read | [1.2](#12-read-a-lab-back) | [`admin_api.py:62`](radreport/api/routes/admin_api.py#L62) |
| POST | `/admin/api/labs` | PA | admin-write · 64 KiB | [1.1](#11-register-the-lab) | [`admin_api.py:80`](radreport/api/routes/admin_api.py#L80) |
| GET | `/admin/api/labs/{tenant_id}/readiness` | PA, S | admin-read | [2.8](#28-readiness-and-the-gate-to-pilot) | [`admin_api.py:96`](radreport/api/routes/admin_api.py#L96) |
| POST | `/admin/api/labs/{tenant_id}/status` | PA | admin-write · 4 KiB | [1.3](#13-the-lifecycle-status-machine) | [`admin_api.py:107`](radreport/api/routes/admin_api.py#L107) |
| GET | `/admin/api/labs/{tenant_id}/steps` | PA, S | admin-read | [2.7](#27-configure-the-engines) | [`admin_api.py:120`](radreport/api/routes/admin_api.py#L120) |
| POST | `/admin/api/labs/{tenant_id}/assignments` | PA | admin-write · 4 KiB | [2.7](#27-configure-the-engines) | [`admin_api.py:131`](radreport/api/routes/admin_api.py#L131) |
| POST | `/admin/api/labs/{tenant_id}/assignments/{assignment_id}/activate` | PA | admin-write · 4 KiB | [2.7](#27-configure-the-engines) | [`admin_api.py:141`](radreport/api/routes/admin_api.py#L141) |
| GET | `/admin/api/labs/{tenant_id}/onboarding` | PA, S | admin-read | [2.8](#28-readiness-and-the-gate-to-pilot) | [`admin_api.py:154`](radreport/api/routes/admin_api.py#L154) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/roster` | PA | admin-upload · 50 MiB | [1.4](#14-add-the-people) | [`admin_api.py:160`](radreport/api/routes/admin_api.py#L160) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/templates` | PA | admin-upload · 50 MiB | [2.1](#21-templates-s1) | [`admin_api.py:169`](radreport/api/routes/admin_api.py#L169) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/corpus` | PA | admin-upload · 100 MiB | [2.2](#22-feed-the-historical-reports-s2) | [`admin_api.py:194`](radreport/api/routes/admin_api.py#L194) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals` | PA | admin-write · 4 KiB | [2.1](#21-templates-s1) | [`admin_api.py:200`](radreport/api/routes/admin_api.py#L200) |
| POST | `/admin/api/labs/{tenant_id}/onboarding/steps/{step}` | PA | admin-write · 4 KiB | [2.0](#20-the-step-runner) | [`admin_api.py:209`](radreport/api/routes/admin_api.py#L209) |
| GET | `/admin/api/labs/{tenant_id}/autonomy/{class_code}` | PA, S | admin-read | [4.5](#45-autonomy) | [`admin_api.py:219`](radreport/api/routes/admin_api.py#L219) |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/open-accrual` | PA | admin-write · 4 KiB | [4.5](#45-autonomy) | [`admin_api.py:229`](radreport/api/routes/admin_api.py#L229) |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/grant` | PA | admin-write · 4 KiB | [4.5](#45-autonomy) | [`admin_api.py:239`](radreport/api/routes/admin_api.py#L239) |
| POST | `/admin/api/labs/{tenant_id}/autonomy/{class_code}/revoke` | PA | admin-write · 4 KiB | [4.5](#45-autonomy) | [`admin_api.py:255`](radreport/api/routes/admin_api.py#L255) |
| POST | `/admin/api/labs/{tenant_id}/adaptation/gates` | PA | admin-write · 1 MiB | [4.6](#46-retrain-the-engines) | [`admin_api.py:270`](radreport/api/routes/admin_api.py#L270) |
| POST | `/admin/api/labs/{tenant_id}/adaptation/require-gates` | PA | admin-write · 1 MiB | [4.6](#46-retrain-the-engines) | [`admin_api.py:279`](radreport/api/routes/admin_api.py#L279) |
| GET | `/admin/api/users` | PA, S | admin-read | [I](#platform-users) | [`admin_api.py:303`](radreport/api/routes/admin_api.py#L303) |
| POST | `/admin/api/users` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_api.py:317`](radreport/api/routes/admin_api.py#L317) |
| POST | `/admin/api/users/{user_id}/deactivate` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_api.py:335`](radreport/api/routes/admin_api.py#L335) |
| POST | `/admin/api/users/{user_id}/reactivate` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_api.py:341`](radreport/api/routes/admin_api.py#L341) |
| POST | `/admin/api/users/{user_id}/password` | PA | admin-write · 4 KiB | [I](#platform-users) | [`admin_api.py:351`](radreport/api/routes/admin_api.py#L351) |

### `/ingest` — capture (1)

| Method | Path | Who | Limit | Phase | Source |
|---|---|---|---|---|---|
| POST | `/ingest/recordings` | R, LA | lab-upload · 512 MiB | [3.1](#31-capture) | [`ingest.py:38`](radreport/api/routes/ingest.py#L38) |

### `/onboarding` — lab-side onboarding (17)

| Method | Path | Who | Limit | Phase | Source |
|---|---|---|---|---|---|
| POST | `/onboarding/radiologists/{radiologist_id}/voice-enrollment` | LA, R | lab-write · 64 KiB | [1.5](#15-voice-and-the-two-consents) | [`onboarding.py:69`](radreport/api/routes/onboarding.py#L69) |
| POST | `/onboarding/radiologists/{radiologist_id}/training-consent` | LA, R | lab-write · 4 KiB | [1.5](#15-voice-and-the-two-consents) | [`onboarding.py:88`](radreport/api/routes/onboarding.py#L88) |
| GET | `/onboarding/templates/candidates` | LA, R | lab-read | [2.1](#21-templates-s1) | [`onboarding.py:112`](radreport/api/routes/onboarding.py#L112) |
| POST | `/onboarding/templates/candidates/{candidate_id}/review` | R | lab-write · 1 MiB | [2.1](#21-templates-s1) | [`onboarding.py:129`](radreport/api/routes/onboarding.py#L129) |
| POST | `/onboarding/merge-proposals/{proposal_id}/decide` | R | lab-write · 4 KiB | [2.1](#21-templates-s1) | [`onboarding.py:145`](radreport/api/routes/onboarding.py#L145) |
| POST | `/onboarding/batches/{batch_id}/apply` | R | lab-write · 4 KiB | [2.1](#21-templates-s1) | [`onboarding.py:157`](radreport/api/routes/onboarding.py#L157) |
| POST | `/onboarding/batches/{batch_id}/revert` | R | lab-write · 4 KiB | [2.1](#21-templates-s1) | [`onboarding.py:175`](radreport/api/routes/onboarding.py#L175) |
| POST | `/onboarding/corpus/mappings/{mapping_id}/verify` | R | lab-write · 4 KiB | [2.2](#22-feed-the-historical-reports-s2) | [`onboarding.py:190`](radreport/api/routes/onboarding.py#L190) |
| GET | `/onboarding/corpus/histogram` | LA, R | lab-read | [2.2](#22-feed-the-historical-reports-s2) | [`onboarding.py:203`](radreport/api/routes/onboarding.py#L203) |
| GET | `/onboarding/corpus/referrer-prior` | LA, R | lab-read | [2.2](#22-feed-the-historical-reports-s2) | [`onboarding.py:210`](radreport/api/routes/onboarding.py#L210) |
| POST | `/onboarding/collision-findings/{finding_id}/resolve` | R | lab-write · 4 KiB | [2.3](#23-mine-the-vocabulary-s3) | [`onboarding.py:222`](radreport/api/routes/onboarding.py#L222) |
| GET | `/onboarding/verbatim/queue` | T, LA, R | lab-read | [2.4](#24-verbatim-annotation-s4) | [`onboarding.py:235`](radreport/api/routes/onboarding.py#L235) |
| POST | `/onboarding/verbatim` | T | lab-write · 1 MiB | [2.4](#24-verbatim-annotation-s4) | [`onboarding.py:254`](radreport/api/routes/onboarding.py#L254) |
| GET | `/onboarding/boilerplate/export` | LA, R | lab-read | [2.5](#25-rank-the-normals-s5) | [`onboarding.py:267`](radreport/api/routes/onboarding.py#L267) |
| POST | `/onboarding/boilerplate/{candidate_id}/promote` | R | lab-write · 4 KiB | [2.5](#25-rank-the-normals-s5) | [`onboarding.py:279`](radreport/api/routes/onboarding.py#L279) |
| POST | `/onboarding/critical-rules` | R | lab-write · 64 KiB | [2.6](#26-author-the-safety-rules-s6) | [`onboarding.py:304`](radreport/api/routes/onboarding.py#L304) |
| POST | `/onboarding/critical-rules/{rule_id}/approve` | R | lab-write · 4 KiB | [2.6](#26-author-the-safety-rules-s6) | [`onboarding.py:316`](radreport/api/routes/onboarding.py#L316) |

### `/review` — the human loop (12)

| Method | Path | Who | Limit | Phase | Source |
|---|---|---|---|---|---|
| GET | `/review/queue` | R, T, LA, A | lab-read | [3.3](#33-take-work-from-the-queue) | [`review.py:68`](radreport/api/routes/review.py#L68) |
| GET | `/review/queue/stats` | R, T, LA, A | lab-read | [3.3](#33-take-work-from-the-queue) | [`review.py:79`](radreport/api/routes/review.py#L79) |
| GET | `/review/drafts/{draft_id}` | R, T, A | lab-read | [3.4](#34-open-a-draft) | [`review.py:91`](radreport/api/routes/review.py#L91) |
| GET | `/review/drafts/{draft_id}/audio` | R, T, A | lab-read | [3.4](#34-open-a-draft) | [`review.py:137`](radreport/api/routes/review.py#L137) |
| POST | `/review/drafts/{draft_id}/revisions` | R, T | lab-write · 1 MiB | [3.5](#35-save-edits) | [`review.py:185`](radreport/api/routes/review.py#L185) |
| POST | `/review/drafts/{draft_id}/sign` | R | lab-write · 4 KiB | [3.6](#36-sign) | [`review.py:198`](radreport/api/routes/review.py#L198) |
| POST | `/review/reports/{report_id}/addendum` | R | lab-write · 1 MiB | [3.7](#37-after-signing) | [`review.py:214`](radreport/api/routes/review.py#L214) |
| POST | `/review/alerts/{alert_id}/acknowledge` | R | lab-write · 4 KiB | [3.7](#37-after-signing) | [`review.py:230`](radreport/api/routes/review.py#L230) |
| POST | `/review/reports/{report_id}/grade` | R | lab-write · 4 KiB | [4.1](#41-grade-the-output) | [`review.py:246`](radreport/api/routes/review.py#L246) |
| GET | `/review/metrics/cse-rate` | R | lab-read | [4.1](#41-grade-the-output) | [`review.py:257`](radreport/api/routes/review.py#L257) |
| POST | `/review/drafts/{draft_id}/usefulness` | R, T | lab-write · 4 KiB | [4.2](#42-ask-whether-the-draft-helped) | [`review.py:273`](radreport/api/routes/review.py#L273) |
| GET | `/review/metrics/usefulness` | R, T, LA, A | lab-read | [4.2](#42-ask-whether-the-draft-helped) | [`review.py:284`](radreport/api/routes/review.py#L284) |

### `/ui` — review screens, HTML (2, plus the public static route)

| Method | Path | Who | Limit | Phase | Source |
|---|---|---|---|---|---|
| GET | `/ui/drafts/{draft_id}` | R, T, A | lab-read | [3.8](#38-the-screens) | [`review_ui.py:99`](radreport/api/routes/review_ui.py#L99) |
| GET | `/ui/queue` | R, T, LA, A | lab-read | [3.8](#38-the-screens) | [`review_ui.py:187`](radreport/api/routes/review_ui.py#L187) |

### `/ga` — lab-side autonomy, export and drift (6)

| Method | Path | Who | Limit | Phase | Source |
|---|---|---|---|---|---|
| GET | `/ga/autonomy/{class_code}` | LA, R, A | lab-read | [4.5](#45-autonomy) | [`ga.py:45`](radreport/api/routes/ga.py#L45) |
| POST | `/ga/autonomy/{class_code}/revoke` | R, LA | lab-write · 4 KiB | [4.5](#45-autonomy) | [`ga.py:71`](radreport/api/routes/ga.py#L71) |
| GET | `/ga/autonomy-coverage` | R, LA, A | lab-read | [4.5](#45-autonomy) | [`ga.py:82`](radreport/api/routes/ga.py#L82) |
| GET | `/ga/export/{report_id}/hl7` | R, LA | lab-read | [4.3](#43-send-it-to-the-hospital-system) | [`ga.py:102`](radreport/api/routes/ga.py#L102) |
| GET | `/ga/export/{report_id}/fhir` | R, LA | lab-read | [4.3](#43-send-it-to-the-hospital-system) | [`ga.py:114`](radreport/api/routes/ga.py#L114) |
| GET | `/ga/drift` | LA, R, A | lab-read | [4.4](#44-watch-for-drift) | [`ga.py:144`](radreport/api/routes/ga.py#L144) |

---

# Appendix B — Known gaps

Things a client developer will look for and not find, in the phase where they
will look.

**Everywhere — the review screen has no sign-in page.** Lab users sign in through
`POST /auth/login` and send a bearer token, which a plain browser opening `/ui/*`
cannot do yet.

**Everywhere — rate limits are per process.** The counters live in each
worker's memory, so with *n* workers a caller gets up to *n* times the
configured rate, and a restart resets them.

**Phase 1 — no `GET /admin/api/labs/{tenant_id}`.** Single-lab status comes
only from filtering `GET /admin/api/labs`, from the `POST .../status` response,
or from the `/admin/labs/{id}` page.

**Phase 1 — no endpoint exposes the legal next statuses.**
[`_ALLOWED_TRANSITIONS`](radreport/core/tenancy.py#L162) reaches the lab page's
status form but no JSON response, so an API client either hard-codes the table
or discovers the boundary by eating a `409`.

**Phase 1 — no per-user CRUD for lab users, and no branding endpoint.** The
roster CSV is the only way a lab user is created or changed, and it never
deletes. `tenant_branding` is inserted empty at registration and never written
again.

**Phase 2 — the corpus has no upload form.** It is loaded through
`POST /admin/api/labs/{tenant_id}/onboarding/corpus` only. The page's step
buttons also run with default options; `min_frequency` and `verified_only` are
API-only.

**Phase 3 — nothing creates a `Study` or a `Patient`.** Yet
[`/ingest/recordings`](radreport/api/routes/ingest.py#L38) requires a
`study_id`. Outside tests, neither type is instantiated anywhere in
`radreport/`.

**Phase 3 — no HTTP trigger for the pipeline.** Nothing under `api/` imports
`pipeline/`; `workers/` is empty. Capture stores audio and returns, and no
draft ever appears. See [3.2](#32-the-missing-link).
