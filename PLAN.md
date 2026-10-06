# Radiology Voice-to-Structured-Report — Implementation Plan

Derived from *Technical Design Document v1.0* (`radiology-reporting-system-design.pdf`,
47pp, complete). The design doc decides **what** to build; this decides **what
order**, **what to cut**, and **what the doc gets wrong or leaves out**. Where
they disagree, the design doc wins on clinical/safety questions and this wins on
sequencing.

---

**Status.** Now scoped as multi-tenant SaaS per owner decision — see **§9**,
which supersedes part of D19. Other owner decisions folded in: microphones confirmed (§4.1), ₹12
baseline is the radiologist assistant's cost (§8.1), compose may go local in
Beta (§8.1). Cost model rebuilt against corrected Sonnet 5 pricing throughout
§0.2 and §7; budgeted on the structurally-guaranteed caching tier (§0.4a),
with the rest treated as upside. R24 resolved in **§10**; admin-panel and
tenancy mechanics in **§11**. Open items in §8.2.

---

## 0. Corrections to the source document

These change numbers or code, so they go first.

### 0.1 §§12–15 now present

The earlier export was truncated at §11.3. The current PDF carries §12 (first
eight weeks), §13 (team), §14 (decision log D1–D23), and §15 (business model).
What they change for this plan:

- **§12 gives a week-by-week schedule.** It has two ordering bugs — see §0.6.
- **§13 assumes 5.2 FTE engineering + 1.2 FTE clinical/ops.** This contradicts
  §15.3 — see §3.
- **§14.4 leaves D14, D15, D21, D22, D23 open**, with owners and deadlines.
  These are folded into §4.
- **§15 makes the cost correction below materially more important**, because
  §15.2a's break-even bar is computed directly from the §11.3 total.

### 0.2 §7.9.2 Sonnet 5 pricing is stale — corrected, with all downstream tables

§7.9.2 prices Claude Sonnet 5 at **$3.00 / $15.00 per MTok**. That was Sonnet
4.6's rate. The current rate is **$2.00 / $10.00**. Opus 5 ($5/$25) and Haiku
4.5 ($1/$5) are correct.

Using the doc's own stated method — 54,000 input words and 5,400 output words
per report, words counted 1:1 as tokens, at ₹96/USD — this reproduces every
other row of §7.9.2 exactly (Opus 38.88≈39, Haiku 7.78≈8, GPT-5.4 20.74≈21,
Gemini 3.1 Pro 16.59≈17, Kimi K2 4.41≈4), so the corrections below are
consistent with the document's own arithmetic rather than a competing model.

**§7.9.2 — corrected row**

| Provider | Model | Input $/M | Output $/M | ₹ per report |
|---|---|---|---|---|
| Anthropic | Claude Sonnet 5 | ~~$3.00~~ **$2.00** | ~~$15.00~~ **$10.00** | ~~~23~~ **~16** |

All other rows stand. Note the shape change: Opus 5 is now **2.5×** Sonnet 5,
not 1.7×, and Sonnet 5 sits below GPT-5.4 and level with Gemini 3.1 Pro rather
than above both. §7.9.2's remark that Sonnet "is not the expensive choice" is
now considerably more true.

**§7.9.3 — blended split.** Solving for the Sonnet share of token volume that
reproduces the doc's own ₹16 figure gives f ≈ 0.53 (53% of tokens on
consequential tasks). At corrected pricing the blend is **₹11.9**, not ₹15–17.
Against a corrected all-Sonnet pipeline of ₹15.6, the Haiku split still buys
roughly a 24% cut.

**§11.1 — per-stage.** LLM lines scale by 2/3; ASR and storage are unchanged.
Totals: V1 **~$0.18** (was ~$0.25), Beta/GA **~$0.46** (was ~$0.65).

**§11.2 / §11.3 — volume-scaled.** The doc's committed-use discount ladder is
preserved (LLM column stepping 16→12, i.e. 1.00 down to 0.75).

| Reports/day | Infra | ASR | LLM | **AI-only** | **+ review (₹12)** | *(doc: AI-only)* | *(doc: +review)* |
|---|---|---|---|---|---|---|---|
| 20 | 5.4 | 3 | 11.9 | **20.3** | **32.3** | 24.4 | 36.4 |
| 50 | 2.2 | 3 | 11.9 | **17.1** | **29.1** | 21.2 | 33.2 |
| 100 | 2.0 | 3 | 11.9 | **16.9** | **28.9** | 21.0 | 33.0 |
| 200 | 1.7 | 3 | 11.1 | **15.8** | **27.8** | 19.7 | 31.7 |
| 500 | 1.7 | 3 | 11.1 | **15.8** | **27.8** | 19.7 | 31.7 |
| 1,000 | 1.5 | 3 | 10.4 | **14.9** | **26.9** | 18.5 | 30.5 |
| 2,000 | 1.4 | 3 | 9.7 | **14.1** | **26.1** | 17.4 | 29.4 |
| 5,000 | 1.2 | 3 | 8.9 | **13.1** | **25.1** | 16.2 | 28.2 |

**§15 — the part that actually moves.**

| | Doc | Corrected |
|---|---|---|
| AI-only @1,000/day (§15.0, §15.4) | ₹18.5 | **₹14.9** |
| AI + review @1,000/day (§15.0, §15.4) | ₹30.5 | **₹26.9** |
| Radiologist time must be worth (§15.2a) | >₹18.5/report | **>₹14.9/report** |
| Seconds saved to break even @₹25–50/min | 25–45 s | **18–36 s** |
| v0.9 changelog range | ₹18.5–30.5 | **₹14.9–26.9** |

**§15.0's conclusion survives.** Every configuration still costs more than the
₹12 baseline — the cheapest corrected row is ₹25.1 (AI+review @5,000/day). The
retraction stands. But the bar §15.2a sets for the pilot drops by about 28%:
the draft needs to save **18–36 seconds**, not 25–45. That is a materially
easier bar, and §15.2a calls this "the most important open item in this
document."

### 0.2a Local open-source models for the small tasks — the column §11.2 never computes

§7.9.4 proposes running the bounded tasks on open-weight models (Llama 3.1 8B
or Qwen 2.5 7B) on existing radiologist workstations, and §11.2 notes the
bounded share "moves to zero marginal cost" past ~300–500/day. Neither puts it
in the table. Here it is, at corrected pricing.

Two scopes, because the doc's bounded set and the obvious one differ:

- **Scope A — §7.9.3's bounded set:** utterance classification, routing
  shortlist ranking, round-trip check. 47% of token volume.
- **Scope B — A + compose ("report generation").** §7.9.3 never buckets
  compose at all; it appears in §11.1 at $0.03 of $0.23 LLM spend, ~13%, and
  in neither §6.14's `task_key` list nor §7.9.4's exclusion list. Compose is a
  reasonable local candidate — its input is restricted to grounded atoms, so
  it cannot introduce content — which puts ~60% of token volume local.

| Reports/day | Infra | ASR | **Cloud** LLM / AI / +rev | **Local A** LLM / AI / +rev | **Local B** LLM / AI / +rev |
|---|---|---|---|---|---|
| 20 | 5.4 | 3 | 11.9 / 20.3 / 32.3 | 8.2 / 16.6 / 28.6 | 6.2 / **14.6** / 26.6 |
| 50 | 2.2 | 3 | 11.9 / 17.1 / 29.1 | 8.2 / 13.4 / 25.4 | 6.2 / **11.4** / 23.4 |
| 100 | 2.0 | 3 | 11.9 / 16.9 / 28.9 | 8.2 / 13.2 / 25.2 | 6.2 / **11.2** / 23.2 |
| 200 | 1.7 | 3 | 11.1 / 15.8 / 27.8 | 7.7 / 12.4 / 24.4 | 5.8 / **10.5** / 22.5 |
| 500 | 1.7 | 3 | 11.1 / 15.8 / 27.8 | 7.7 / 12.4 / 24.4 | 5.8 / **10.5** / 22.5 |
| 1,000 | 1.5 | 3 | 10.4 / 14.9 / 26.9 | 7.2 / **11.7** / 23.7 | 5.4 / **9.9** / 21.9 |
| 2,000 | 1.4 | 3 | 9.7 / 14.1 / 26.1 | 6.7 / **11.1** / 23.1 | 5.0 / **9.4** / 21.4 |
| 5,000 | 1.2 | 3 | 8.9 / 13.1 / 25.1 | 6.2 / **10.4** / 22.4 | 4.6 / **8.8** / 20.8 |

Bold marks AI-only below the ₹12 incumbent baseline. **Scope B clears it from
50 reports/day upward** — the pilot's own volume — and Scope A from 1,000/day.

**"Zero marginal cost" assumes hardware you already own and IT approves.**
§7.9.4's premise is a background service on a PACS-connected radiologist
workstation with spare capacity. If that approval doesn't come and you buy a
box, amortise it: ~₹2L capex over 3 years plus ~400 W continuous at ₹8/kWh is
~₹95k/yr ≈ ₹259/day. Scope B then reads ₹27.6 (20/day), ₹13.8 (100), ₹11.8
(200), ₹10.2 (1,000), ₹8.9 (5,000) — still under ₹12 from ~200/day.

**§11.2's 300–500/day threshold is not a compute-cost threshold.** On
arithmetic alone, dedicated hardware beats the cloud bounded-task spend it
replaces at roughly **60–100 reports/day** (₹2.59/report of hardware at 100/day
vs ₹3.66 of Haiku). The doc's higher number is really about setup and
maintenance effort — which §15.3 identifies as the binding constraint for a
self-funded founder. So **300–500 remains the right threshold for this
project, but for the reason §15.3 gives, not the one §11.2 states.** Worth
correcting, because if a second engineer ever joins, the number moves.

**Two cautions before adopting Scope B.**

1. **Do not put compose and the round-trip check on the same local model.**
   §8.3.8's round-trip entailment is what catches a compose that drops or
   garbles a grounded atom. An 8B model checking an 8B model's work removes
   the independence the check depends on. If compose goes local, keep
   `roundtrip_check` on cloud Haiku.
2. **At V1 there is no round-trip check at all** — §4.1 defers the verifier to
   Beta. Local compose in V1 would be unverified. Move compose local in Beta,
   once the verifier exists to catch it.

**§7.9.4's scope discipline still holds:** extraction, self-correction, and
verification stay on Sonnet regardless of local hardware capability. And every
swap goes through §7.9.6's gate — `task_model_assignment` cannot reach
`active` without a gold-set `eval_run`. The table above is a hypothesis about
cost; only the eval says whether accuracy survives.

**One more local candidate, off this table:** S2's report→template mapping
derivation (§6.10). It is bulk, offline, latency-insensitive, and runs over
thousands of historical reports at onboarding — the best possible fit for a
local model, and it never appears in a per-report cost figure because it isn't
per-report. Same for lexicon mining and boilerplate ranking.

**Does this rescue the business case?** No. §15.0's comparison is the
delivered system, and +review keeps every configuration above ₹12 (cheapest:
₹20.8). But §15.0a says a commercial offering is contingent on finding a
configuration that changes the conclusion, and Scope B at ₹9.9–11.4 AI-only is
the first one on the board. Combined with the prompt-caching lever in §0.4 and
GA autonomy removing review from ≥40% of volume (§4.3), the delivered number
has a plausible path toward ₹12 that none of the document's current
configurations have.

### 0.3 Model identifiers must NOT carry date suffixes

§6.14 gives `claude-sonnet-5-20260415` as the example value for
`model_definition.model_identifier`, and §8.5 says to pin dated versions. The
current API rejects date-suffixed IDs — the correct strings are
`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`, complete as written.

§8.5's *intent* (a vendor-side change is a pipeline change that must clear the
release gate) is right and must be kept. Implement it by pinning in
`model_definition` + recording the resolved ID on every `stage_execution`, and
by treating a provider changelog entry as a release event — not by inventing a
version string the API will reject.

### 0.4 Two cost levers missing from §11

**Prompt caching.** Extraction runs k=3 samples × N sections per report, each
with an identical prefix (system prompt + compiled template JSON schema +
retrieved exemplars). That prefix is most of the 54,000 input words. Cache
reads bill at a small fraction of base input. Order the prompt as
`[stable: system, schema, exemplars] → [cache breakpoint] → [volatile:
transcript]` **from the first extraction call written** — retrofitting means
restructuring every prompt and re-running the gate. Input is ≈2/3 of the LLM
bill. How much of it caches splits into a guaranteed tier, a design choice,
and an unknown — see **§0.4a**, which is the part to act on.

**Batch API (50%).** Applies to three workloads here — release-gate eval,
archive reprocessing, and the routine production tail. Not to critical-findings
detection or stat/urgent, which must stay live. Full treatment in §7.6.

### 0.4a Caching: what is guaranteed, what is a design choice, what is unknown

The cacheable fraction varies by stage and template and cannot be pinned
before the prompts exist. But it decomposes into three tiers, and only the
third is genuinely unknown.

**Tier 1 — guaranteed, needs no cross-report stability.** Extraction runs
**k=3 samples of an identical prompt** (§8.3.5; only temperature/seed vary).
Samples 2 and 3 are exact prefix repeats. Extraction is ~65% of LLM spend and
is input-heavy, so this alone is **~29% of the LLM bill: ₹11.89 → ₹8.40,
AI-only ₹13.40.** No design care required beyond one breakpoint at the end of
the prompt.

> **One implementation trap.** The cache is written when a call *completes*.
> Firing all k samples concurrently means 2 and 3 arrive before the entry
> exists and all three miss. Fire sample 1, await it, then fan out 2 and 3.
> The §2 latency budget (60–180 s) absorbs the extra round trip easily; this
> is the difference between 29% and 0%.

**Tier 2 — a design decision, not a measurement.** The prompt layers, most to
least stable:

| Layer | Stable across | Cacheable? |
|---|---|---|
| System prompt + provenance rules (§8.4) | everything | Always |
| Compiled template JSON schema | all reports on that template | Always |
| Section instruction | all reports on (template, section) | Always |
| Few-shot exemplars | **depends — see below** | Conditional |
| Transcript with char offsets | nothing — per report | Never |

§8.4 specifies exemplars as *retrieved* nearest neighbours. Caching is
prefix-match, so **if exemplars are re-retrieved per report they invalidate
every layer after them** and Tier 2 collapses to zero. If instead they are
**pinned per (template, speaker) and refreshed on a schedule**, they become
stable and cache across every report on that template — taking LLM to
**~₹4.66, AI-only ~₹9.66.**

That is the single largest lever in the cost model, and it is a choice rather
than a discovery. It carries a small accuracy question — per-report retrieval
may match marginally better — which is exactly what the gold set is for. Test
pinned-vs-retrieved as a release-gate comparison in week 6.

**Tier 3 — genuinely unknown.** How much of the *non-extraction* stages
(routing, specialists, compose, verify) shares a stable prefix. Varies by
stage and template. Measure, don't estimate.

**Planning rule:** budget §7 on Tier 1 only (₹13.40 AI-only). Treat Tier 2 as
upside to be confirmed, and order every prompt `stable → breakpoint →
volatile` from the first one written, so the upside stays available.

### 0.5 Sonnet 5 API shape

For the extraction/verification stages: `thinking: {type: "adaptive"}` plus
`output_config.effort`; `budget_tokens` is rejected. Assistant prefill is
removed. Structured output is `output_config: {format: {...}}`, and tool
schemas take top-level `strict: true` — this is how §8.3.5's "provider
JSON-schema mode" is actually spelled. Context is 1M (Sonnet 5) / 200K
(Haiku 4.5), so §9.10's long-recording chunking is an ASR-side concern only.

---

### 0.6 Two ordering bugs in §12

**Bug 1 — onboarding is scheduled before the schema it writes to.** §12 puts
"Onboarding S0–S2 built first" and the corpus load in **week 1**, but
"Schema + migrations; ingest API; queue" in **week 3**. S0–S2 write
`import_batch`, `import_artifact`, `corpus_report`,
`corpus_report_template_map`, and `template_import_candidate`. Those tables do
not exist until week 3.

**Bug 2 — the ASR bake-off is scheduled before the gold set it runs on.** §12
puts the bake-off in **week 3**. The `current` partition only *begins*
recording in week 2, and "Gold annotation complete, both partitions" is
**week 4**. §5.3 is explicit that all forward-looking decisions — "including
the week-3 ASR bake-off" — use the `current` partition. A week-3 bake-off can
only run on `legacy` audio, which is precisely the failure §5.3 and R14 exist
to prevent, and which §12's own header says microphones moved to week 1 to
avoid.

Both are fixed by the phasing in §2: foundations before onboarding, gold set
before the bake-off.

## 1. The dependency chain that decides build order

The V1 scope list in §4.1 is presentation order, and §12's week-by-week plan
violates it twice (§0.6). The real chain:

```
  infra + schema + stage contracts + LLM adapter + model registry
        │
        ▼
  S0 roster ──▶ S1 templates ──▶ S2 corpus ──▶ S3 lexicon (pass 1)
                                                     │
                            ASR adapter ─────────────┤
                                                     ▼
                                              S4 paired audio
                                                     │
                                       ┌─────────────┴─────────────┐
                                       ▼                           ▼
                            S3 lexicon (pass 2)          verbatim annotation
                                       │                  (HUMAN — longest lead)
                                       ▼                           │
                                 S5 / S6 / S7 ◀────────────────────┘
                                       │
                                       ▼
                          eval harness runnable → ASR bake-off → pipeline
```

Three consequences the design doc does not draw out:

**1. The ASR adapter is Phase-0 infrastructure, not a pipeline stage.** S4's
bootstrap ASR run needs it, and S4 gates the gold set, which gates everything.
Building it "when we get to the pipeline" stalls onboarding for weeks.

**2. Verbatim annotation of the 150 `current` gold items is the critical
path.** Microphone purchase is confirmed, so this is human labor that cannot
begin until the new mics are deployed *and* enough dictations have accumulated
on them. Everything forward-looking — the bake-off, every release gate, every
threshold — waits on it. Therefore:

> **Ship a capture-only ingest path in week 1.** Record → validate → FLAC →
> object store → `recording` row. No pipeline, no ASR, no UI. It exists purely
> so the `current` partition accumulates while onboarding is being built.

**3. Two work items need no software at all and should start day 1:**
- Verbatim annotation of the 100 `legacy` gold items (existing archive).
- The §7.8.2b baseline audit — 50 already-signed reports graded G0–G4. It
  produces `baseline_cse_rate`, which every §8.3.10 non-inferiority
  calculation depends on, and it gates S7.

---

## 2. Phases

### Phase 0 — Foundations

Nothing clinical ships here; everything downstream assumes it.

- Repo per §8.1. Python 3.12, FastAPI, Pydantic v2, SQLAlchemy + Alembic.
- Postgres 16 + pgvector. Full §6 schema by migration.
- **`tenant_id` + RLS from the first migration** (D19). Cheap now, a rewrite
  later.
- `Stage` / `StageResult` / `PipelineState` contracts (§8.2). No stage writes
  domain tables; the orchestrator commits. This is what makes replay and
  shadow mode honest.
- `pipeline_run` / `stage_execution` persistence; `audit_log` on an
  INSERT-only DB role.
- Object storage adapter (S3-compatible, SSE-KMS). FLAC/WAV only, enforced at
  ingest.
- **LLM adapter + `model_provider` / `model_definition` /
  `task_model_assignment` (§6.14)** — build the *resolver* now, the *screen*
  later. Every call site asks "which model serves task `extraction`?"; none
  names a model. Skipping this means hardcoding `claude-sonnet-5` in seven
  places and unpicking it in Beta. Cloud and local OpenAI-compatible endpoints
  are the same row shape.
- **ASR adapter interface** + one engine behind it.
- Eval harness skeleton: §6.9 tables, one module per §5.2 metric,
  `is_release_gate` logic. Per invariant I6 this precedes the pipeline.
  **Design `eval_run` to be scopeable to a single `task_key` from the start** —
  §7.9.6's per-task model swap is impossible otherwise. **Build the executor
  stage-synchronised so gate runs can go on the Batch API** (§7.6); this
  halves eval spend, which during the build rivals production (§7.3).
- Observability: Langfuse tracing, per-stage cost accounting,
  `pipeline_run.total_cost_usd` budget cap with hard abort.
- **Capture-only ingest** (see §1 above). Quality gates from §9.8 live here:
  sample rate <16 kHz, lossy codec, duration bounds, SNR floor, silence ratio.
- Synthetic data generator for local dev — **no PHI on developer machines.**

**Exit:** a recording can be uploaded, validated, stored, and audited. Nothing
interprets it yet.

---

### Phase 1 — Knowledge onboarding (S0–S7)

§7.8. This is the product's first module and its scaling mechanism (R17), not
a pile of import scripts.

| Stage | Build | Human gate |
|---|---|---|
| S0 Roster | CSV/HR import, dedup, voice enrollment + DPDP consent capture | Consent |
| S1 Templates | .docx/.rtf parse → `template_import_candidate` → dedup proposals → routing card → `spoken_study_code` | Radiologist per schema, per merge, per code |
| S2 Corpus | Bulk load, derive report→template map, usage histogram, referrer prior, exemplar index | Hand-verify ≥200 mappings |
| S3 Lexicon | Mine terms, double-metaphone keys, **collision audit**, polysemy review | **Blocking:** every `severity='block'` resolved |
| S4 Paired audio | Link studies, queue verbatim transcription, bootstrap ASR, mine surface variants | Transcriptionists produce verbatim |
| S5 Boilerplate | Rank normals by `corpus_share` | Deferred — see cuts |
| S6 Critical rules | Seed from corpus urgency language | Radiologist authors, sets SLA + escalation |
| S7 Readiness | Run all §7.8.3 checks | Pilot go/no-go |

Non-negotiables: nothing reaches `template_version` without approval (R18);
idempotent by `content_hash`; applying a batch creates new versions rather than
mutating (this is also the §9.10 rollback path); `is_deidentified` gates every
external API call.

The **S3↔S4 loop is designed, not accidental** — run S3 with declared shorthand
only, run S4 ASR, mine observed variants, re-run S3. Two passes converge. Build
S3 re-runnable from the start.

**Exit:** S7 green on all `fail`-severity checks.

---

### Phase 2 — Eval harness live + bake-offs

- Gold set frozen, **split by `capture_device_class`** (§5.3). The `legacy`
  partition evaluates archive reprocessing only; every forward-looking
  decision uses `current`. R14 is rated "high likelihood if unaddressed" and
  this split is the entire mitigation.
- **ASR bake-off** on `current`: Deepgram Nova-3 Medical, Azure Speech custom,
  Whisper medium, Whisper large-v3. Track `INS_RATE` as an independent metric,
  never folded into `WER` — §7.6 reports large-v3 at 19.0% WER vs medium's
  13.2% on Indian-accented speech, with insertions at 50.7% of large-v3's
  errors. Bigger is not better here.
- **LLM bake-off** for consequential tasks: Sonnet 5 vs Kimi K2 Thinking, on
  the same gold set, same harness. Do not adopt on price.
- Code-switching probe (§9.7): if any radiologist drops into Hindi or a
  regional language, monolingual English ASR produces confident nonsense
  rather than failing. Test explicitly; it changes engine choice for those
  speakers.

**Exit:** engines and models chosen on *your* audio, with numbers.

---

### Phase 3 — V1 pipeline

Build in graph order; each stage is independently measurable against the gold
set before the next one starts.

1. Preprocess — VAD, SNR, normalise
2. Single ASR + keyterm biasing from the active lexicon set
3. **Normalise** — phonetic resolution (§8.3.2). Deterministic. The margin
   guard (`best − second < TAU_MARGIN` → escalate) is what stops LMC/LMP
   resolving silently (R12).
4. **Study-code detection** (§8.3.9) — bounded search in the first ~20 s
   behind a fixed carrier phrase. Instrument `CODEWORD_COMPLIANCE` (did they
   say it) separately from `STUDYCODE_RECALL` (did we hear it) — different
   problems, different fixes, and only the first decides D1.
5. Segment + classify (LLM) — labels only, **nothing deleted** (I2)
6. **Resolve repairs** (§8.3.3) — "left — sorry, right kidney". Naive
   filtering keeps `left`. G4 error class. Retracted spans are marked
   `superseded_by_id`, never removed, and render struck-through.
7. **Critical findings + alerting** (§9.1) — runs on the *transcript*, fires
   before anything enters a queue, bypasses the queue entirely. Tune for
   recall at any precision cost. **This ships with the queue, not after it** —
   the queue is what creates the exposure.
8. Finding sketch (LLM, template-free)
9. **Route cascade** (§8.3.4) — study code → hard filter → demographic →
   hybrid rank → LLM picks from top-5 → attach modules → **coverage
   validation**. Top ~20 templates only at V1. Orphan assertions are the
   wrong-template signal.
10. **Extract** per section, k=3 at temp 0.3, provenance required (§8.3.5)
11. **Ground + coverage** — deterministic, outside the LLM. Quote must appear
    verbatim in the cited span; source utterance must be
    `is_included_downstream`. Ungrounded fields never render.
12. Compose — input restricted to grounded atoms
13. **Verify** — schema + the deterministic rules in §8.3.8 only at V1
14. Confidence (§8.3.7) — `min` over critical fields × `mean` over all.
    Averaging hides exactly the case that matters.
15. Route to human

**Absence handling (§8.3.6) is the highest-liability logic in the system.**
Every field defaults to `leave_blank_flag`. Pass 1 (week 5, ~30 min) asks the
radiologist only which fields are `is_critical` — never auto-filled under any
circumstance. `default_normal` promotion is Pass 2, post-pilot, driven by
observed reviewer behaviour. Nothing auto-fills in V1 by construction (R4).

---

### Phase 4 — Review web app

§7.2 calls this "the product surface that determines adoption." R7 and R8 are
both about humans, and the flywheel every later phase depends on runs through
this screen.

- Per-field **click-to-listen** off `provenance_span.audio_start_ms`
- Retracted spans struck through with the override shown
- **`fill_source != 'dictated'` visually distinct** — this is the system
  asserting something no human said
- Blanket-normal phrase shown beside every field it filled
- Flagged-first ordering
- **`active_edit_seconds` — focus time, not wall clock.** This is the primary
  value metric input and the §15.2 argument rests on it. Instrument it
  properly or the commercial case has no evidence.
- G0–G4 grading UI on the §5.4.1 sampling schedule (every report weeks 1–4,
  then 1-in-5, 1-in-10, 1-in-20)
- One-click **"this draft was useless"** (§9.6) — and actually track it
- RBAC: transcriptionist / radiologist / admin / auditor are genuinely
  different (§9.10)

---

### Phase 5 — Beta (months 3–6)

Multi-engine fan-out + ROVER reconciliation with LLM arbitration on disputed
spans only; phonetic post-correction; LLM critic + round-trip entailment
(tuned for recall — the §8.3.8 precedent is 100% recall at 76.9% precision,
with `human_verdict` tightening it over time); per-speaker profiles; full
template library; shadow mode; **autonomy accrual, observation only**; DICOM
**conditional on the §4.4 trigger rule** — ≥95% compliance after four weeks
defers it indefinitely, <90% pursues it.

### Phase 6 — GA (months 6–12+)

Autonomy grant/revocation engine (§8.3.10 — Bayesian sequential grant,
mechanical CUSUM revocation); HL7 v2 ORU^R01 / FHIR export; ASR adaptation
behind all six §8.6.5 gates; distilled classifier + router; amendments;
drift monitoring.

---

## 3. What I would cut or defer from V1

| Item | Call | Why |
|---|---|---|
| S5 boilerplate UI | Defer | §8.3.6 makes it Pass 2 / post-pilot by design. A CSV export suffices for the pilot. |
| Model Config **screen** | Defer to Beta | The *tables and resolver* are Phase 0 and non-negotiable; the UI is not needed until there is a second model to consider. |
| LLM specialist agents | Defer | Keep measurement/unit and laterality as **deterministic validators** in V1 — that is where the value is. LLM specialists are Beta. |
| Full template library | Defer | Top ~20 by `usage_count_12m` covers the power-law head. |
| Local open-source hosting | Defer to Beta | Not for the reason §11.2 gives — on compute it pays from ~60–100/day (§0.2a). Defer because it is founder-time the pilot cannot spare, and because Scope B (local compose) needs the round-trip verifier that V1 doesn't have. |

**Do not cut:** critical-findings detection (regulatory, R9), the collision
audit (R12), provenance enforcement (I1), the hardware-split gold set (R14),
or `active_edit_seconds` instrumentation.

**Scope realism — §13 and §15.3 contradict each other, and it decides the plan.**

§13 staffs this at **5.2 FTE engineering/data plus 1.2 FTE clinical/ops**.
§15.0a and §15.3 say it is a **personal project**, that "engineering cost is
founder time, not a headcount line," and that "at self-funded scale, cutting
scope matters more than optimizing per-report cost, because founder time is
the binding constraint."

Both cannot be true. §15 is the later section — v1.0's changelog adds §15.0a —
so the founder-time framing is the current reality and §13 is aspirational.

The consequence is concrete: **§12's eight-week schedule is a 5-FTE plan.** At
one founder plus part-time clinical help it is not achievable, and §4.1's exit
criteria (≥30% minute reduction, ≥1,500 edit pairs) go with it. On founder
time, Phases 0–2 — foundations, onboarding, gold set, bake-offs — are a realistic
first eight weeks, with the pilot following rather than landing in week 8.

This is worth resolving explicitly rather than discovering in week 6, because
§15.3 already gives the right instruction for the founder case: **cut scope,
don't optimise cost.** Applied to §12 that means the pilot-20 templates, one
ASR engine, no local hosting, and the review UI built properly — which is what
§2 phases toward.

---

## 4. Week-1 blockers that are not code

These gate the schedule and none of them are engineering tasks. §14.4's still-open
decisions are folded in with their doc-assigned owners and deadlines.

1. **Order the microphones — resolved, now an execution item** *(week 1,
   Ops + Eng)*. D2 confirmed: new dictation mics will be bought; D23 is
   closed. Prefer push-to-talk (§9.10 — a record button solves most of the
   aside problem in hardware rather than in a classifier). This is now the
   longest-lead item in the project: §5.3's `current` gold partition cannot
   start accumulating until the mics are deployed, the partition gates the
   week-3 ASR bake-off, and the bake-off gates the pipeline (§1). Order in
   week 1, deploy in week 2, and run the capture-only ingest path (§1) behind
   them so recordings accumulate while onboarding is built.

2. **D22 — accession number field mapping** *(week 1, Eng + Hospital IT)*.
   D9 confirms accession numbers exist per report; D22 notes the concrete
   field name in upload metadata is still unchecked. Without it, RIS filing at
   GA is impossible — independent of the DICOM routing question.

3. **D12 — baseline audit** *(§7.8.2b, during S2)*. 50 already-signed reports
   graded no-error / minor / significant. Produces `baseline_cse_rate`, which
   every §8.3.10 non-inferiority calculation depends on and which gates S7.
   §14.2 marks it specified but **not yet executed**. Pure human work on
   existing data — it can start immediately and needs no software.

4. **D21 — voice-enrollment consent wording** *(week 2, Legal + Ops)*.
   A voiceprint is biometric data under DPDP. D18 blocks S0 roster onboarding
   for any radiologist until signed. **Draft it as two consents, not one** —
   enrollment for diarization and training use are separate purposes (§10.3).
   Cheap while D21 is still open; a second consent round later is not.

4a. **Pooling clause + patient-notice wording** *(Legal, before contract #1)*.
   The two genuinely irreversible items in §10.8. Free to include now,
   near-impossible to retrofit across signed labs.

5. **D15 — transcriptionist role evolution** *(week 2, Product + Ops)*.
   R7 rates flywheel stall from disengagement as High impact. §9.6 is blunt
   that the usual failure here is not technical. Deferred by request; revisit
   before week 2.

6. **D14 — critical-findings rule set** *(week 6, Radiologist champion)*.
   Which findings, which SLAs, which escalation contacts. Gates the §9.1 alert
   path, which ships with the queue.

7. **Template + historical corpus export** from the client — gates S1/S2.
   D3 makes the pilot 20 a week-1 radiology deliverable; D16 confirms
   Word/PDF only, so no RTF or RIS-export parser is needed.

8. **Is ₹12/report wage-only or fully loaded?** (§15.0, §15.5 lists it first).
   Plus the radiologist loaded cost per minute, which §15.4 marks *still
   unconfirmed* — the corrected 18–36 second break-even bar in §0.2 is derived
   from an estimated ₹25–50/min and should not be treated as real until that
   figure is measured.

## 5. Test and CI strategy (absent from the design doc)

The doc specifies measurement thoroughly and testing not at all.

- **Tenant-leak test in CI.** RLS is only real if a test proves a
  cross-tenant query returns zero rows. D19 upgraded isolation from "column
  now" to "enforced now" — that promise needs a test.
- **Eval-set leakage test.** §6.12 puts a schema `CHECK` on
  `excludes_eval_set`. Assert it fires. R21 is rated Critical and silent.
- **"Exactly one active assignment per task."** §6.14 notes this rule lives in
  application code because Postgres `CHECK` cannot reference another table —
  so it needs a test, and it is called out for code review.
- **Golden-file tests** for every deterministic stage (normalise, ground,
  rules, confidence). These must be bit-reproducible.
- **Replay tests** from stored `stage_execution` rows — the contract in §8.2
  exists to make this possible.
- **Property tests** on the phonetic index, specifically E-set confusions
  (B/C/D/E/G/P/T/V/Z), which is where LMC/LMP sit.
- **Idempotency tests**: duplicate `content_hash`, ASR rerun with identical
  `config_hash`, re-uploaded import artifact.

---

## 6. Gaps I would add to the design

- **Manual fallback path.** §9.4 names the requirement — "reports must still
  get produced by the existing manual path" when the pipeline is down — with
  no design. This needs an explicit operator kill switch and a documented
  degraded mode, not an implied one.
- **Vendor concurrency control.** Rate limiting and backpressure against ASR
  and LLM providers. §8.5 covers outage and circuit-breaking but not
  saturation.
- **Global starting lexicon seed.** `lexicon_term.tenant_id` is nullable
  specifically to allow one (§6.13). A RadLex/SNOMED subset gives S3 something
  to mine *against* rather than starting empty.
- **Prompt-level eval separate from pipeline eval** — required for §7.9.6's
  per-task swap to mean anything.
- **Cost attribution per template and per radiologist** (§9.10) — cheap while
  `stage_execution` is being written, awkward to backfill.

---

## 7. What it costs: setup, running, and the optimisation ladder

All figures exclude human cost — the radiologist assistant, the radiologist,
and founder time. See §8.3 on role naming.

### 7.1 One-time setup — loading the corpus is a rounding error

| Item | ₹ |
|---|---|
| ASR bake-off — 4 engines × 150 items ± keyterms | 2,304 |
| S2 corpus mapping — 5,000 historical reports (Haiku) | 840 |
| S4 bootstrap ASR — 250 paired items | 480 |
| S3 lexicon curation (mostly deterministic) | 200 |
| S5 boilerplate ranking (aggregation) | 100 |
| S1 parse 20 templates (Sonnet) | 50 |
| **Total** | **~₹4,000** |

Corpus mapping scales linearly with archive size: ₹336 at 2,000 reports,
₹1,680 at 10,000, ₹4,200 at 25,000. Embeddings run on a local model, so ₹0.

**Budget ₹5,000 and stop thinking about it.** The expensive part of onboarding
is human — verbatim annotation of 250 gold items, radiologist template
approval, and the D12 baseline audit — not compute.

### 7.2 Daily and monthly running cost at 100 reports/day

26 working days/month = 2,600 reports. Infra is a **fixed ~₹6,100/month**, not
per-report — §11.2's ₹2.00/report framing hides that, and it means infra does
not scale down if pilot volume comes in under 100/day.

| Config | ₹/day | ₹/month | ₹/report |
|---|---|---|---|
| as designed — live, no caching | ₹1,724 | ₹44,814 | ₹17.24 |
| + Tier-1 caching (§0.4a) | ₹1,375 | ₹35,740 | ₹13.75 |
| + Tier 1, routine 80% batched | ₹1,039 | ₹27,004 | ₹10.39 |
| + Tier 1, all production batched | ₹955 | ₹24,820 | ₹9.55 |
| + Tier 2 (pinned exemplars), 80% batched | ₹814 | ₹21,170 | ₹8.14 |

### 7.3 Eval is recurring, not a setup cost

A release-gate run is 150 gold items through the full pipeline. It is **not** a
one-time onboarding activity: §7.7 requires a gate on every release, §7.9.6
one per model swap, §9.2 one whenever a vendor updates a model underneath you.
Eval cost tracks **how often you change things**, not report volume.

| Phase | Gate runs | Batched, Tier 1 | Batched, Tier 2 |
|---|---|---|---|
| Active build — per 2-month pilot | ~30 | ₹18,900 | ₹10,485 |
| Steady state — per month | ~3 | ₹1,890 | ₹1,048 |

Eval is a natural **Tier 2** case: all 150 gold items share the same system
prompt, schemas and exemplars, and the same prompts repeat across every run.

Unbatched and uncached the pilot figure is ₹53,500, against ₹61,800 of
production LLM spend over the same period. That is the trap: during
active development **eval rivals production**, and it is invisible in §11
because §11 is a per-report table.

Three fixes, all cheap:

1. **Run the gate on the Batch API** (§7.6) — half price, and a gate run has
   no latency requirement at all.
2. **Split the gate.** A ~30-item smoke subset for iteration, the full 150
   only on release candidates.
3. **Cache the eval prefix.** All 150 items share system prompt, schemas and
   exemplars.

### 7.4 Two-month pilot envelope at 100/day

52 working days, 5,200 reports, 2 calendar months of infra, 30 gate runs.

| Config | LLM prod | LLM eval | Infra | **Infra + LLM** | ASR | All-in |
|---|---|---|---|---|---|---|
| as designed (no caching, live eval) | ₹61,828 | ₹53,505 | ₹12,200 | **₹127,533** | ₹15,600 | ₹143,133 |
| + Tier-1 caching, live eval | ₹43,680 | ₹37,800 | ₹12,200 | **₹93,680** | ₹15,600 | ₹109,280 |
| + Tier-1 caching, eval batched | ₹43,680 | ₹18,900 | ₹12,200 | **₹74,780** | ₹15,600 | ₹90,380 |
| + Tier-2 caching, eval batched | ₹24,232 | ₹10,485 | ₹12,200 | **₹46,917** | ₹15,600 | ₹62,517 |

7-day operation runs ~17% higher on the variable lines. Add ~₹5,000 one-time
(§7.1) and ~15% for retries and reprocessing.

**Plan on the Tier-1 + batched-eval row: ~₹85,000–95,000 for two months.**
That basis is structurally guaranteed (§0.4a) rather than estimated. Tier 2
would take it to ~₹55,000–65,000, but it needs a gold-set check first, so
treat it as upside rather than budget. Without caching or batching: ~₹165,000.
The spread is discipline, not architecture — same models, same accuracy.

**ASR is not the assistant.** The ₹15,600 ASR line is a vendor speech-to-text
API (Deepgram/Azure per §7.6 of the design doc). Machine cost, stays in. What
is excluded is the human at ₹12/report.

### 7.5 Where cost optimisation is possible, ranked

At 100/day, baseline AI-only **₹16.89**. This table keeps §11.2's per-report
infra of ₹2.00 so the rows stay comparable with the design doc; §7.2's
₹17.24/report uses the fixed-monthly treatment instead. The infra constant is
identical across rows, so every delta and percentage below is unaffected.

| Lever | LLM ₹ | AI-only ₹ | Cut | Risk |
|---|---|---|---|---|
| baseline — cloud Sonnet + Haiku | 11.89 | 16.89 | — | — |
| **+ caching, Tier 1 only — k-sample repeats (§0.4a)** | 8.40 | **13.40** | 21% | None — structurally guaranteed |
| **+ caching, Tier 2 — exemplars pinned (§0.4a)** | 4.66 | **9.66** | 43% | Needs a gold-set check on pinned vs retrieved |
| + local Scope B instead (§0.2a) | 6.21 | 11.21 | 34% | Needs verifier; Beta |
| + Scope B **and** Tier-1 caching | ~4.02 | **~9.02** | 47% | Beta |
| + Scope B **and** Tier-2 caching | ~3.23 | **~8.23** | 51% | Beta |
| *(alt)* Kimi K2 on consequential tasks | 5.99 | 10.99 | 35% | Unmeasured on your data |

The two Scope B + caching rows are marked approximate: they compound a
per-stage decomposition (§11.1) with a per-task one (§7.9.3) that the design
doc never reconciles, so treat them as direction and magnitude, not as
two-decimal figures.

**Input is 67% of the LLM bill** (₹7.93 of ₹11.89), which is why caching
dominates the list. Extraction runs k=3 samples × N sections, each re-sending
system prompt + template schema + exemplars. That prefix is the cacheable part.

Levers not on the table, in rough order of value:

- **Drop k from 3 to 2** on template classes with proven low self-consistency
  disagreement. Extraction is ~65% of LLM spend, so this is worth ~20% of the
  LLM bill. Accuracy trade — must clear the gate per class, not globally.
- **Effort tuning.** Sonnet 5 exposes `output_config.effort`. Extraction at
  `medium` rather than the default `high` cuts thinking tokens. Free to test,
  measurable on the gold set.
- **Skip the LLM routing pick** when the §8.3.4 Stage-3 retrieval margin is
  large. Deterministic fast path on the easy majority; LLM only on close
  calls.
- **Keep specialists deterministic** (already in §3) — ~13% of LLM spend in
  §11.1 that never needs a model.
- **Batch API for archive reprocessing** (§0.4) — 50% off a workload that is
  pure batch by nature.
- **ASR is ₹3 flat, 18% of cost.** Self-hosting Whisper zeroes it but buys GPU
  ops; not worth it at 100/day. Negotiate committed-use rates instead.

**The headline:** Tier-1 caching alone — the k-sample repeats, which are
structurally guaranteed rather than estimated — takes cloud V1 to
**₹13.40/report with zero accuracy risk and no local hardware.** Pinning
exemplars (Tier 2) takes it to **₹9.66, under the ₹12 incumbent baseline**, for
the price of one gold-set comparison. Neither is mentioned in §11.

Do it before the prompts are written. Retrofitting means restructuring every
prompt and re-running the gate, and the ordering rule costs nothing to follow
up front.

---

### 7.6 What "batched" means, and where it must not be used

**Batch API.** Requests are submitted asynchronously instead of called live,
at **50% off input and output**, returning within 24 hours (usually far
sooner). Same models, same outputs. The only thing traded is immediacy.

**Design consequence for the eval harness (Phase 0).** A whole pipeline run
cannot be one batch job — stage N+1 needs stage N's output. But the 150 gold
*items* are independent. So the harness fans out **per stage, across items**:
submit all 150 extraction calls as one batch, wait, then all 150 verification
calls. The eval executor is stage-synchronised, not item-parallel. Build it
that way from the start — converting later means rewriting the executor.

```python
batch = client.messages.batches.create(requests=[
    {"custom_id": f"{item.id}::extract::{section}", "params": {...}}
    for item in gold_items for section in item.template.sections
])
# poll until processing_status == "ended", then:
for r in client.messages.batches.results(batch.id):
    results[r.custom_id] = r   # results arrive in ANY order — never index by position
```

That last line is the real gotcha: indexing by position silently misattributes
outputs to gold items, which corrupts metrics rather than crashing.

**Batching production is the bigger prize — and has a hard safety boundary.**
It is worth ~₹9,000/month against eval's ~₹1,500 (§7.2). But:

- **Critical-findings detection must never be batched.** §9.1 runs detection
  on the transcript so alerts fire *before* the draft enters any queue, and
  the design is explicit that the risk is created the moment reports start
  queueing. A pneumothorax sitting undetected in a 24-hour batch is precisely
  the failure §9.1 exists to prevent. **ASR and critical-findings detection
  stay on the live path, always** — which is why ASR is ₹3.00 in every row of
  §7.2.
- **stat/urgent cannot be batched.** §9.4 requires priority ordering;
  `study.priority` is already in the schema. Route those live.

That yields the split: live ASR + critical detection for everything, live full
pipeline for stat/urgent (~20%), batch for the routine tail (~80%) →
**₹9.49/report**. The ₹0.69 gap to fully-batched is what safety costs, and it
is worth paying.

**Before defaulting routine to batch, measure current turnaround.** If the
assistant returns reports in ~2 hours today and a batch can take 24, that is a
service regression the client will feel whatever it saves (§8.2).

**Also batch-shaped:** archive reprocessing (§7.8's "ASR improved" trigger,
thousands of historical reports) — the other pure-batch workload, and part of
why §6.11 retains audio indefinitely.

### 7.7 Does DICOM change the cost? Three scenarios

**No — per-report cost is effectively identical in all three.** DICOM supplies
*metadata*, not images: modality, body part, study description, protocol,
contrast agent (§6.2, §7.2). That is ~50 tokens added to a prompt.

| Scenario | Routing anchor | ROUTE_TOP1 (§4.4) | ₹/report vs baseline |
|---|---|---|---|
| **Recording only** (V1 as designed) | spoken study code → lexical → demographic | 95–98% without code, ~99.5% with | baseline |
| **Recording + DICOM** | DICOM hard filter on `study_description` | ~99.9% | **−₹0.19** |
| **Recording + DICOM + spoken code** | both, cross-validated | ~99.9%+ | **−₹0.19** |

Where the small saving comes from: with a DICOM `study_description` lookup,
§8.3.4's Stage 1 becomes a deterministic hard filter and the Stage-4 LLM pick
can be skipped on the confident majority. That pick is ~3.5% of LLM spend, so
skipping 80% of them saves ~₹0.19/report — **₹500/month at 2,600 reports.**
Carrying the metadata itself costs ₹0.01/report. Both are noise.

**DICOM's cost is fixed, not variable.** A sync service (C-FIND or DICOMweb
QIDO-RS, nightly batch per §7.2), PACS network access, and hospital IT
security approval. That is an integration project — engineering time and
approvals — which is exactly why §4.4 defers it behind a measured trigger:
half a percentage point of routing accuracy does not pay for it on its own.

**What the third scenario buys that the second doesn't:** two *independent*
routing anchors that must agree. A spoken-code / DICOM mismatch is a strong
wrong-study or wrong-patient signal — useful against R5 (cross-contamination
from a neighbouring dictation) and §9.10's session-recording safety net.
Neither anchor alone gives you that check. It costs nothing extra and is worth
wiring in if DICOM ever lands.

**One thing to be explicit about: DICOM images are not in scope, and must not
creep in.** If the pipeline ever ingested pixel data the economics invert
completely — one key image ≈ ₹0.29/report, twenty slices ≈ ₹5.76, a full
series ≈ ₹57.60 on Sonnet vision tokens, against a whole-pipeline budget of
₹9–17. Reading images is also a different regulatory product (§9.5 — it is no
longer transcription assistance). Metadata only.

---

## 8. Open questions

### 8.1 Resolved

| # | Question | Resolution |
|---|---|---|
| 1 | Buy microphones (D2 vs D23)? | **Yes — mics will be bought.** D2 stands, D23 closed. §5.3's hardware-split gold set is load-bearing; the `current` partition is the critical path (§1). |
| 2 | §13's 5.2 FTE or §15.3's founder time? | Set aside at owner's direction. §2's phasing is unchanged; revisit only if the week-8 pilot date starts slipping. |
| 3 | Is ₹12 wage-only or fully loaded? | **₹12 is the cost of that role, now titled *radiologist assistant* rather than transcriptionist.** See §8.2 for the one part that remains open. |
| 4 | Reissue §11? | Done — corrected tables in §0.2, local-hosting columns in §0.2a, pilot budget in §7. |
| 5 | Is compose a bounded task? | **Yes, with conditions.** It can run on a local model as Scope B (§0.2a), but only in Beta and only with `roundtrip_check` kept on cloud Haiku — V1 has no verifier to catch a bad compose. |

### 8.2 Still open

1. **Does ₹12 cover the whole radiologist-assistant role, or only its
   transcription share?** If the assistant also does scheduling, chasing
   priors, patient prep and so on, the AI displaces only part of that time —
   so ₹12 is the right *baseline for the typing work* but not the amount
   recoverable. §11.3 and §15.0 both compare against the full ₹12. Worth
   pinning before the break-even math in §0.2 is used commercially.
2. **Radiologist loaded cost per minute.** §15.4 marks it *still unconfirmed*
   at ₹25–50/min, and the corrected 18–36 s break-even bar (§0.2) is derived
   from it. Measure it, or the bar is an estimate on an estimate.
3. **Current *human* turnaround time — the incumbent's, not ours.** Three
   different times get conflated here:

   | | What it is | Status |
   |---|---|---|
   | **Incumbent turnaround** | dictation → signed report **today**, via the assistant | **Unknown — this is the bar** |
   | AI compute latency | the pipeline's own processing time | Budgeted: 60–180 s (§2) |
   | AI end-to-end turnaround | dictation → signed, through the AI path incl. queue + review + signature | Target: <10 min p95 at GA (§4.3) |

   Only the first is unknown, and it is the one that matters: batching changes
   AI compute latency, which flows into AI end-to-end, which must not be worse
   than the incumbent. **Fold the measurement into the D12 baseline audit** —
   it already pulls 50 signed reports for CSE grading (§7.8.2b), so record two
   timestamps per report, dictation and signature. Zero extra effort, and it
   settles whether routine batching is viable before anyone builds for it.
4. **Cacheable fraction — varies by stage and template, and is not knowable
   yet.** Confirmed by the owner. So the plan stops treating it as a planning
   number and treats it as two things instead: a **prompt-layout rule** that
   holds regardless of the fraction (§0.4), and a **measurement** taken on the
   first real extraction calls in week 6 via `usage.cache_read_input_tokens`.
   Budget on the conservative row of §7.5, not the optimistic one. What is
   *structurally* guaranteed rather than estimated is set out in §0.4a.

### 8.3 Terminology note

The design doc says "transcriptionist" throughout, including as an enum value
in `report_revision.reviewer_role` and `final_report.path_type` (§6.7). The
role is **radiologist assistant** in this deployment. Keep the schema values as
the doc defines them to avoid diverging from the source — change the display
labels in the review UI instead.
---

## 9. Multi-tenant SaaS scope (owner decision, supersedes part of D19)

Per-lab model configuration, lab-admin self-service report upload, and
white-label branding. **This is a scope change, not a clarification**, and it
should be recorded as one.

### 9.1 What it supersedes

§6.13 (D19) lists, under "still deferred, and correctly so":

- Per-tenant billing/metering
- **Per-tenant admin panel or self-service onboarding**
- Any UI concept of "switching tenants"

— all "gated on a second client actually signing, not built speculatively
now." Two of those three are now in scope. That is a legitimate decision to
change, but D19's reasoning was sound, so the cost of reversing it should be
visible rather than absorbed silently. Branding/white-label appears nowhere in
the design doc at all.

### 9.2 §6.14's model config is single-tenant as designed — schema deltas

The admin panel tables carry **no `tenant_id`**, and the live-assignment
constraint is global:

```sql
-- §6.14 as written: exactly one active assignment per task, system-wide
CREATE UNIQUE INDEX ... ON task_model_assignment (task_key)
  WHERE status = 'active';
```

Per-lab configuration requires:

| Table | Change |
|---|---|
| `model_provider` | `+ tenant_id uuid NULL` — NULL = a global provider offered to all labs |
| `model_definition` | `+ tenant_id uuid NULL`; `endpoint_override` becomes genuinely per-tenant (each lab's local box has its own address) |
| `task_model_assignment` | `+ tenant_id uuid NOT NULL`; unique index becomes `(tenant_id, task_key) WHERE status = 'active'` |
| `task_model_assignment_log` | `+ tenant_id uuid NOT NULL` |
| `eval_set` / `eval_run` | `+ tenant_id uuid NULL` — NULL = canonical set, set = a per-lab acceptance set (§9.3) |

All four get RLS policies per §6.13. Do this in the **Phase 0 migration**, not
later — the resolver ("which model serves task `extraction`?") becomes
("...for tenant T?"), and every call site changes. That is cheap now and a
refactor across the whole pipeline later.

**Superseded in part by §11.3:** tenancy is now universal, so these are no
longer deltas against a single-tenant schema but instances of a general rule —
with `model_provider`, `model_definition` and `eval_set` among the few tables
that stay *nullable* rather than `NOT NULL`. §11 is authoritative on tenancy
mechanics; read it before writing the migration.

### 9.3 Two gold sets, not one per lab

With model control held by the product admin, the §7.9.6 gate becomes **your**
obligation rather than a blocker on a lab's settings screen. That removes the
scaling problem — but it raises a design question §5.3 never had to answer,
because §5.3 assumes a single client.

**Does every new lab need its own 250-item gold set before go-live?** If yes,
onboarding costs ~80–100 person-hours of verbatim transcription per lab and
the business does not scale. The answer is no, but you need two distinct sets
rather than one:

| Set | Owner | Size | Purpose |
|---|---|---|---|
| **Canonical gold set** | You | 250, growing additively (§9.9) | Model and pipeline release gates. Pooled across labs, stratified by modality × template × speaker × audio quality × **tenant** |
| **Per-lab acceptance set** | Per tenant | ~40 items | S7 readiness only: does the pipeline actually work on *this* lab's accents, mics, and templates? |

The acceptance set answers a narrower question than the canonical set, so it
can be much smaller. It feeds `gold_set_frozen` in §7.8.3 and nothing else —
it is not a release gate.

**And it gets cheaper with every lab, via §8.6.4's own cost curve.** At lab #1
verbatim means typing from scratch: 40 items ≈ 13 person-hours. By lab #3 a
decent draft exists, so verbatim becomes "verify and lightly correct ASR
output" — five to ten times cheaper per minute of audio. **That is roughly
half a day per lab**, against two-plus weeks if you insisted on a full gold
set each time. Onboarding cost falls as the flywheel turns, which is what
makes this scalable at all.

Two consequences to build for now:

- **`eval_set` needs `tenant_id` *nullable*** — NULL for the canonical set,
  set for a per-lab acceptance set. Not `NOT NULL` as §9.2 first suggested.
- **`eval_item` must stay stratifiable by tenant**, so a canonical gate run
  can report per-lab breakdowns. A model change that helps eight labs and
  breaks the ninth must be visible as such, not hidden in an average — the
  same argument §5.2 makes for `ROUTE_TOP1` being "reported per template,
  never averaged only."

### 9.3a Cross-tenant training pooling needs to be in the contract

This follows directly and is easy to miss. §8.6.3's adaptation roadmap depends
on accumulating ≥20 h of verbatim audio on current hardware, and §8.6.5's
`G3_speaker_balance` gate requires ≥5 speakers with none exceeding 40% of
corpus hours. **At one lab you may never clear that bar; pooled across labs you
clear it easily.**

But §6.11 establishes a dual legal basis — clinical retention is statutory,
model-training retention is a separate purpose under DPDP purpose limitation
needing its own basis and disclosure. Per tenant, that means
`recording.is_training_corpus_eligible` is gated on **that lab's** contractual
permission.

So: **put cross-tenant training-data pooling in the lab agreement from the
first contract.** Retrofitting consent across signed clients is close to
impossible, and without it the §8.6 adaptation roadmap — the thing that makes
the product better over time rather than merely cheaper — never starts. Note
this is distinct from the cross-tenant *analytics* that §6.13 defers, and it
should not be lumped in with it.

### 9.4 The permission split

Confirmed by the owner: **product admin owns every model decision; lab admin
gets branding, SaaS administration, and data upload.** No middle tier — the
"conditionally exposable bounded tasks" idea is dropped, and it should stay
dropped. It was the one part of a settings screen that could quietly degrade a
clinical pipeline, and removing it means no lab-facing surface can.

**Lab admin — the whole surface:**

| Setting | Backed by |
|---|---|
| Branding: logo, colours, report letterhead, subdomain | new (§9.5) |
| Users, roles, roster; voice enrollment + consent | S0, §6.2 |
| **Upload historical reports** → lexicon, exemplars, routing priors, template mapping | S2, `import_batch` |
| **Upload historical audio + matched reports** → acceptance set, surface variants, threshold calibration | S4 |
| Templates: import, review dedup, approve, version | S1, `template_import_candidate` |
| Shorthand / lexicon review, collision resolution | S3 |
| Boilerplate → `default_normal` promotion | S5, §8.3.6 Pass 2 |
| Critical-findings rules, SLAs, escalation contacts | S6, radiologist-approved |
| `absence_policy` and `is_critical` per field | §8.3.6 Pass 1 |
| Notification and queue-priority preferences | §9.4 of design doc |

Note the clinical items here are gated on **that lab's radiologist**, not on
the lab admin alone — S1, S5 and S6 are radiologist-approval gates in the
design and stay that way. The lab admin operates lab-side onboarding; the lab's
radiologist signs off on what it produces.

**Product admin — everything else:**

model assignment per task *and per tenant* (cloud or local, §9.2), pipeline
and prompt versions, `k` and thresholds, release gates, autonomy grants, the
canonical gold set, tenant provisioning.

**What the uploaded data is and isn't for.** §8.6.1 is emphatic that nothing
is trained at onboarding, and the reasons are unchanged per tenant: signed
reports are not token-aligned to audio, and a lab's archived audio is from
whatever hardware they used before. So an upload feeds **configuration and
measurement** — lexicon terms and surface variants, retrieval exemplars,
routing and referrer priors, the derived template mapping, threshold
calibration, and the per-lab acceptance set (§9.3). It does not tune a model.

That also means uploaded audio lands in the **`legacy` capture-device
partition** for that tenant (§5.3), and `G4_hardware_homogeneous` (§8.6.5)
keeps it out of any adapter that will serve production. Worth enforcing in
the importer rather than trusting the label, since R20 rates this "high
likelihood if attempted early."

### 9.5 Branding / white-label — entirely new

Not in the design doc. Minimum viable:

- `tenant_branding`: logo asset ref, primary/accent colours, report header and
  footer text, letterhead block, subdomain, sender name for alert emails.
- `template_version.render_spec` already carries "field order, headings, house
  style" (§6.5), so house style is per-tenant already. What is new is
  **letterhead and logo on the rendered report**, plus the review UI shell.
- Alert email/SMS templates per tenant (§9.1 escalation paths are per-tenant
  already).

One thing to think through rather than build: the rendered report is a legal
medical record (§6.6 `final_report` is immutable with a `content_hash`).
Producing documents under another organisation's branding puts your output
inside their liability chain. Worth a line in the contract and a mention in
the §9.5 legal read the design doc already calls for before Beta.

### 9.6 Also now required, that D19 deferred

- **Per-tenant cost attribution and metering.** `pipeline_run.total_cost_usd`
  exists; it needs a tenant rollup. This is not optional once labs pick their
  own models, because **their model choice changes your cost per report** —
  which forces a pricing-shape decision (§15.5 defers it): flat per-report and
  you absorb the variance, or cost-plus and you pass it through.
- **Tenant lifecycle**: provisioning, suspension, offboarding and data export.
  §6.11's erasure cascade is defined per patient, not per tenant.
- **Cross-tenant leak tests in CI** (§5) become the highest-value test in the
  suite rather than a precaution, and now must cover the §6.14 tables too.

### 9.7 Where this lands in the phasing

| Phase | Addition |
|---|---|
| **Phase 0** | Universal `tenant_id` + RLS policies + **composite FKs** (§11.3–11.4); `platform_user` realm (§11.2); tenant-scoped model resolver; RLS coverage and leak tests in CI (§11.5). |
| **Phase 1** | Onboarding S0–S7 already per-tenant (§6.13) — add the lab-admin-facing wrapper and a de-identification gate before any LLM-assisted parse. **Cx registration + `tenant.status` lifecycle gated on S7 readiness** (§11.6); admin panel org-selection with audit (§11.1). |
| **Phase 4** | Branding in the review UI shell and the report renderer. |
| **Beta** | Product-admin model config panel (§7.9.6) with per-tenant assignment. Per-tenant metering and cost attribution. |
| **GA** | Self-service provisioning, billing, offboarding export-then-purge cascade. |

**Still not in scope, and should stay out:** cross-tenant analytics or
benchmarking, physical per-tenant data-space separation (§6.13 keeps this as a
later client-managed migration), and any lab-facing model control at all.

**One thing to get right in Phase 1.** Lab-side onboarding is now a
customer-facing product surface, not an internal tool. §7.8.4's design rules
were written for engineers operating it — "nothing goes live without
approval," idempotent by content hash, every import versioned and reversible.
Those matter more, not less, when a stranger is driving it. In particular
§9.10's **onboarding rollback** ("if an applied `import_batch` turns out to be
wrong, there must be a path back — the UI for it still has to exist") moves
from a nice-to-have to a requirement, because you will not be there to fix a
bad import by hand.

### 9.8 New risks

| # | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R23 | A model change validated on the canonical gold set degrades accuracy at one specific lab whose accents, mics or templates are under-represented in it | **Critical** | Medium | Per-tenant stratification of `eval_item` (§9.3); gate runs report per-lab breakdowns, never only an average; per-lab acceptance set re-run after any consequential-task change |
| R24 | Cross-tenant training pooling has no contractual basis, so §8.6's global ASR adapter can never be built | Medium (was High) | High if unaddressed | **Resolved in §10.** Scope corrected: it gates the *global* adapter only, not V1 or per-speaker adaptation (§10.1). Mitigation is the consent chain (§10.2), two-consent split (§10.3), derived eligibility (§10.4), and per-speaker fallback (§10.7) |
| R25 | A lab admin's uploaded legacy audio reaches a production ASR adapter | High | Medium | Force `capture_device_class = 'legacy'` at import; `G4_hardware_homogeneous` enforced in `training_corpus_snapshot`, not by convention (R20) |
| R26 | White-labelled reports place your output inside a lab's liability chain | Medium | Medium | Contract language; part of the §9.5 legal read the design doc already requires before Beta |
| R27 | `tenant_id` present on every table but FKs allow a child row to reference a parent in another tenant — a leak that both the FK and RLS pass | **Critical** | Medium | §11.4 composite foreign keys; CI assertion that no FK crosses tenants; must land in the Phase 0 migration, not after tables populate |

**R23 is the one that replaces the original concern.** The risk was never
really that a lab admin would break things — it is that *you* will change a
model for nine labs on evidence drawn mostly from three of them. The
mitigation is structural: never read a gate run as a single number.

---

## 10. Addressing R24 — the legal basis for pooled training data

R24: *cross-tenant training pooling has no contractual basis, so §8.6's
adaptation roadmap can never start.* This section resolves it. Nothing here is
legal advice — it is the brief to take into the legal read that §9.5 already
requires before Beta.

### 10.1 First, right-size it: pooling buys speaker diversity, not hours

§8.6.5 gates any ASR adaptation on six conditions. Two decide this question:

- **G1_volume** — ≥20 h verbatim audio for a global adapter, ≥5 h for a
  per-speaker one.
- **G3_speaker_balance** — ≥5 speakers, none exceeding 40% of corpus hours.
  **Global adapter only.**

Volume turns out not to be the constraint:

| | Verbatim accrual | 20 h reached | 5 h reached |
|---|---|---|---|
| One lab, 100/day, 10% of reports yielding verified verbatim | 0.67 h/day | ~30 days | ~8 days |
| One lab, at 25% | 1.67 h/day | ~12 days | ~3 days |
| Three labs pooled, 10% | 2.0 h/day | ~10 days | ~3 days |

A single 100/day lab clears G1 for a global adapter in about a month. **G3 is
what pooling actually solves** — it needs five or more distinct dictating
radiologists, and §12's pilot runs with *two*. A lab with three still fails the
speaker count even at perfect balance; five balanced speakers is the floor.

So the honest framing: **R24 does not threaten V1, and it does not threaten
per-speaker adaptation. It gates the global ASR adapter**, which §8.6.3 places
at month 6+ anyway. Urgency comes from the clause being free to include now and
effectively impossible to retrofit — not from the build depending on it.

### 10.2 The consent chain

Under the DPDP Act 2023 the roles are: the **patient** is the Data Principal,
the **lab** is the Data Fiduciary (it collects and sets the purpose), and
**you** are a Data Processor acting on the lab's instructions.

The problem is that a Processor cannot repurpose data for its own ends. Model
training is a new purpose, and determining it arguably makes you a Fiduciary
*for that purpose* — which needs a basis traceable to the patient, not merely
the lab's say-so. §6.11 already states this: training retention "is a separate
purpose under DPDP purpose-limitation and needs its own basis and disclosure."

So the chain has to close at all four links:

| Link | Instrument | Status |
|---|---|---|
| Patient → lab | The lab's patient privacy notice / imaging consent covers model improvement | **You must supply the model wording**; labs will not draft it |
| Lab → you | Pooling clause in the lab agreement, with a warranty that the notice above is in force | New — §10.4 |
| Radiologist → you | **Separate** training consent, distinct from voice enrollment | Gap — §10.3 |
| You → enforcement | Per-tenant gating in the data model, not a spreadsheet | §10.4 |

A lab warranty alone is the weak version: you would be relying on the lab
having actually done the patient-facing part, while still carrying exposure for
having set the purpose. Supplying the notice language is what makes the warranty
worth anything.

### 10.3 D18/D21 cover enrollment, not training — two purposes, two consents

D18 requires "a signed form/checkbox before enrollment, since a voiceprint is
biometric data under DPDP," and D21 leaves the wording open at week 2.

But consenting to a **voiceprint for diarization** (§6.2
`radiologist_profile.voice_embedding`, used to identify the primary speaker) is
a different purpose from consenting to have **your voice train a model that
serves other organisations**. DPDP purpose limitation means two purposes need
two consents. The design doc has one field, `voice_consent_ref`, and it is
scoped to enrollment.

**Add `training_consent_ref` alongside it**, and fold both into the D21 drafting
task at week 2 — while it is still an open drafting item and costs nothing.
Radiologists who decline training use can still be enrolled; their audio is
simply never training-eligible.

### 10.4 Schema and enforcement

A contract clause nobody enforces is R16 waiting to happen. Concretely:

**`tenant`** (§6.13 defines it as `id, name, status, created_at`) gains:

```
training_pooling_consent    boolean NOT NULL DEFAULT false
training_consent_ref        text NULL          -- contract artefact
patient_notice_version      text NULL          -- which notice wording the lab adopted
consent_effective_from      timestamptz NULL
consent_withdrawn_at        timestamptz NULL
```

**`training_consent_event`** — new, append-only, same discipline as
`audit_log`: `tenant_id`, `event` in (`granted`, `renewed`, `withdrawn`),
`ref`, `actor_id`, `occurred_at`. You will need to prove what was permitted
when, years later.

**`recording.is_training_corpus_eligible` becomes derived, not set.** Today it
is a plain boolean anyone can flip. It should be computable only as:

```
tenant.training_pooling_consent
  AND recording.uploaded_at BETWEEN consent_effective_from AND COALESCE(consent_withdrawn_at, 'infinity')
  AND radiologist has training_consent_ref
  AND phi_scrub_completed_at IS NOT NULL      -- §10.5
```

Enforce it in the importer and re-derive on consent change; do not leave it as
a field a future script can set optimistically.

**`training_corpus_snapshot`** (§6.12) gains
`tenant_consent_verified_at timestamptz NOT NULL`. It already carries an
immutable `content_hash` and the `excludes_eval_set` CHECK — extend that same
schema-level rigour here, because §6.12's own argument applies: make it
impossible in the schema rather than a rule someone has to remember.

**Strengthen G6_legal_basis.** §8.6.5 currently reads "every item has
`is_training_corpus_eligible = true`." That is now circular, since eligibility
is derived. G6 should independently assert: tenant consent was live at every
item's `uploaded_at`, every speaker has a training consent, and PHI scrub
completed — recorded in `prerequisite_gates_passed`.

### 10.5 The problem the design doc does not raise: spoken PHI in the audio

§8.4 is careful that patient names never reach an external API — prompts carry
`patient.pseudonym`, age and sex only. But that protects the *text* path. The
**audio** is retained indefinitely as lossless FLAC (§6.11), and if a
radiologist says "this is Mrs Sharma's abdomen scan," that identifier is in the
training corpus forever. Pseudonymisation downstream does not reach it.

This matters more for a pooled corpus than a single-tenant one, because the
audio crosses an organisational boundary.

Two mitigations, both available:

1. **The study-code carrier convention already helps.** §8.3.9 has every
   dictation open with "Study type: ..." rather than patient identifiers. That
   is a habit, not a guarantee, but it reduces exposure at the most likely
   point.
2. **Scrub before pooling, using assets you already have.** The verbatim
   transcript is token-aligned to audio, and `lexicon_term.term_type` already
   includes `person` (§6.5). Detect person-name spans in the verbatim
   transcript, then mute the corresponding audio spans in the *training copy*
   only — the clinical archive stays intact per §6.11's two-store design.
   Record completion in `phi_scrub_completed_at`.

### 10.6 Withdrawal is not reversible, and the contract must say so

Model weights cannot be un-trained. §6.11's erasure cascade says "audio
purged; `final_report` retained per medical records law" — but a model already
trained on that audio persists, and no amount of deletion changes it.

So the lab agreement and the patient notice both need to state plainly that
**permission for already-completed training runs is irrevocable, while future
inclusion stops on withdrawal.** Anything vaguer creates an obligation you
cannot satisfy.

What you *can* do, and should commit to: `training_corpus_snapshot` is
immutable and content-hashed, so you can state exactly which runs included a
given tenant's data and when. That is a real, auditable answer to a withdrawal
request even though un-training is impossible.

### 10.7 If pooling is refused anyway — the fallback

Not existential. In order of preference:

1. **Per-speaker adapters.** G1 needs only ≥5 h per radiologist (~8 days at
   100/day) and **G3 explicitly does not apply** — it is global-adapter-only.
   §8.6.3 already schedules per-speaker work at GA+. A single lab with the
   radiologist's own consent can do this with no pooling at all.
2. **Single-lab global adapter**, for any lab with ≥5 dictating radiologists.
   Clears G1 and G3 without crossing a tenant boundary.
3. **No weight adaptation.** §8.6.3's own verdict: "most of your accuracy in
   the first six months comes from configuration, not weights" — lexicon,
   keyterm biasing, phonetic correction, exemplars. V1 and Beta are unaffected.

### 10.8 Do this week

| Action | Owner | Why now |
|---|---|---|
| Add the pooling clause + lab warranty to the agreement template | Legal | Free before contract #1, near-impossible after |
| Draft patient-notice wording for labs to adopt | Legal | Closes the patient → lab link; labs will not write it |
| Fold `training_consent_ref` into the D21 consent drafting | Legal + Ops | D21 is open at week 2; two purposes, two consents (§10.3) |
| Add the `tenant` consent columns and `training_consent_event` to the Phase 0 migration | Eng | Cheap now; backfilling consent state is guesswork |
| Make `is_training_corpus_eligible` derived in the importer | Eng | Stops R16 at the schema rather than in review |

The clause and the notice wording are the two items that are effectively
irreversible if skipped. Everything else here can follow the build.

---

## 11. Admin panel architecture (org-scoped) and universal tenancy

Three owner decisions: the admin panel takes an `orgId` and configures against
it; every table carries `tenant_id`; lab (Cx) registration lives in the admin
panel. Each has a consequence worth building for deliberately.

### 11.1 "The admin panel takes an orgId" collides with §6.13's RLS model

§6.13 specifies RLS "keyed to **the session's `tenant_id`**" and states that
"a user session is scoped to exactly one tenant at login." A product-admin
panel that selects an org and then configures it is, by definition, a session
that can reach more than one tenant — the "UI concept of switching tenants"
that §6.13 defers.

Do **not** resolve this with `BYPASSRLS` on an admin role. That makes every
admin-side bug a total-disclosure bug. Resolve it by giving the admin session
the *same* policy shape as a lab session, and only the ability to set the
variable:

```sql
-- identical policy for lab users and product admins
CREATE POLICY tenant_isolation ON study
  USING (tenant_id = current_setting('app.current_tenant_id')::uuid);

-- lab session:   app.current_tenant_id set at login, immutable thereafter
-- admin session: set explicitly on "select org", one org at a time, logged
```

The property that matters: **a product admin still cannot see two tenants in
one query.** Picking org B replaces org A rather than widening scope. An
admin-code bug then leaks at most one tenant, and the invariant "no query runs
without a tenant filter" survives for every principal.

Every `SET app.current_tenant_id` from an admin session writes an `audit_log`
row (`actor_type = 'user'`, action `admin_org_selected`). That log is how you
answer "who looked at which lab's data, when."

**Narrow, explicit exceptions** — the cross-tenant reads the admin panel genuinely
needs: the lab list (`tenant` table), aggregate metering rollups, and the
canonical gold set. Keep these in a separate, named set of views with their own
access path. Enumerate them in one file. Anything not on that list is
tenant-scoped, no exceptions.

### 11.2 Product admins are not `app_user` rows

§6.13 requires `app_user.tenant_id NOT NULL`. A product admin belongs to no
tenant, so putting them in `app_user` breaks that constraint and the
one-tenant-per-session invariant with it.

Use a **separate `platform_user` realm** for product admins — own table, own
auth path, own audit trail. Two benefits beyond schema tidiness: a lab admin
can never be escalated to product admin by editing a `roles` array, and the
tenant-scoped tables keep their `NOT NULL` constraint honestly.

**Naming.** §6.2's `app_user.roles` already contains `admin`. That is a *lab*
admin. With a product-admin tier the word is overloaded — rename to
`lab_admin` in the enum, and reserve "admin" for the platform realm. Cheap
now, confusing forever if left.

### 11.3 "Every table has tenant_id" — with a short, explicit exception list

The right default, and the exceptions must be deliberate rather than
discovered. Five tables cannot be `NOT NULL`:

| Table | Tenancy | Why |
|---|---|---|
| `tenant` | none — it *is* the tenant | — |
| `platform_user` | none | §11.2 |
| `model_provider`, `model_definition` | **nullable**, NULL = global catalog | Otherwise the Claude Sonnet 5 row is duplicated per lab and a price change touches N rows |
| `lexicon_set`, `lexicon_term` | **nullable** | §6.13 already decided this: "nullable only for a global starting lexicon" |
| `eval_set`, `eval_item` | **nullable**, NULL = canonical | §9.3 — forcing NOT NULL destroys the pooled canonical gold set and with it §10.1's argument |
| `training_corpus_snapshot` | **nullable** | Pooled by design (§10.1); a cross-tenant snapshot is the whole point |
| `audit_log` | **nullable** | System actions have no tenant |

Everything else: `tenant_id uuid NOT NULL` plus an RLS policy.

### 11.4 The detail that decides whether isolation actually holds

**Foreign keys do not enforce tenant consistency.** With `tenant_id` on every
table you can still have `report_draft.tenant_id = A` referencing
`recording.tenant_id = B`. The FK is satisfied; the rows disagree. RLS will not
catch it either, because each row passes its own policy. That is a silent
cross-tenant leak in the one table §6.13 says "a leak would matter most."

The fix is composite foreign keys, which requires a redundant unique
constraint on the parent:

```sql
ALTER TABLE recording    ADD CONSTRAINT recording_id_tenant_uk UNIQUE (id, tenant_id);
ALTER TABLE report_draft ADD CONSTRAINT report_draft_recording_fk
  FOREIGN KEY (recording_id, tenant_id) REFERENCES recording (id, tenant_id);
```

Apply it on every FK that crosses between tenant-scoped tables. It is tedious
and it is the difference between "we have `tenant_id` everywhere" and
"isolation is enforced by the database." Do it in the Phase 0 migration —
adding composite FKs to populated tables later means validating every row.

### 11.5 RLS coverage must be tested, not assumed

~45 tables means ~45 policies. Hand-written, the forty-sixth table someone adds
will have none, and nothing will fail. Two tests, both cheap, both in CI:

1. **Coverage test.** Enumerate `pg_class` for the tenant-scoped set; assert
   every table has `relrowsecurity = true` *and* at least one policy. A new
   table with no policy fails the build.
2. **Leak test.** Seed two tenants, set `app.current_tenant_id` to A, and
   assert every tenant-scoped table returns zero of B's rows. This is §5's
   tenant-leak test, now the highest-value test in the suite.

Add a third once §11.4 lands: assert no row references a parent with a
different `tenant_id`, across every composite FK.

**Index ordering.** If every query filters on `tenant_id`, composite indexes
should lead with it — `(tenant_id, recording_id, seq)`, not
`(recording_id, seq)`. Retrofitting index column order is a rebuild on tables
§6.11 already expects to partition monthly (`asr_segment`, `edit_event`,
`audit_log`). Keep those partitioned by month as designed; tenancy belongs in
the index, not the partition key.

### 11.6 Cx (lab) registration and the tenant lifecycle

§9.7 had tenant provisioning at GA. It moves to Phase 0/1 — you cannot onboard
lab #2 without it. What registration does:

1. Create the `tenant` row, including the §10.4 consent columns. **Capture
   whether the signed contract included the pooling clause** — this is the
   enforcement point for §10, and the one moment the answer is actually known.
2. Create the lab's first `lab_admin` user; send enrollment invites.
3. Seed defaults: global lexicon set reference, per-task model assignments
   (product-admin chosen, §9.4), branding defaults.
4. Open an `import_batch` with `trigger = 'initial_onboarding'` (§6.10 already
   defines that value).
5. Create the lab's empty acceptance `eval_set` with `tenant_id` set (§9.3).

**`tenant.status` should be an enumerated lifecycle, not free text.** §6.13
defines the column without values. Define them, and tie the important
transition to a gate that already exists:

```
provisioning -> onboarding -> pilot -> live -> suspended -> offboarded
```

**`onboarding` cannot advance to `pilot` until S7 readiness passes** (§7.8.3 —
`collision_audit_clear`, `gold_set_frozen`, `critical_rules_approved`, and the
rest). That turns the readiness report from a checklist someone is meant to run
into a structural precondition. It is the single highest-value thing to wire
into registration, because it makes every future lab onboard correctly by
default rather than by diligence.

`suspended` routes all reports to the manual fallback path (§6, §9.4 of the
design doc) rather than failing them. `offboarded` triggers the export-then-
purge path — which §6.11 defines per patient and not per tenant, so that
cascade still needs writing.
