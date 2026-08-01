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

