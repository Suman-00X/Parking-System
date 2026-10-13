# TODO

## Onboarding: move large imports to S3 + a background job

Today `load_corpus_file`, `submit_template_files`, the gold-bundle ZIP (`import_gold_bundle_file`) and the mining steps that scan the whole corpus (`lexicon-mine`, `collision-audit`, `boilerplate-mine`, `derive-map`) run synchronously inside the admin's HTTP request (`api/routes/admin_panel.py`). This causes:
- request timeouts on the free Render instance;
- the whole file held in memory;
- the corpus file is never kept;
- no retry;
- the batch status machine (`uploading → parsing → …`) is never seen.

Audio ingestion already uses the target pattern (S3 `object_key` + a `run_pipeline` job).

- [ ] **In the request:** check the file type, the `report_text` header and the size; return 422 if bad.
- [ ] **Store the upload in S3** at `tenants/{lab}/imports/{batch}/{content_hash}`. Record it with `register_artifact(..., object_key=...)`.
- [ ] **Queue the job in the same transaction as the batch row:** `enqueue("import_corpus", {"batch_id": ...}, tenant_id=..., dedupe_key=str(batch_id))`. Return `202 {batch_id}`.
- [ ] **Worker handler `@handler("import_corpus")`:**
  - batch → `parsing`;
  - stream from S3, parse in chunks of 1–5k rows, bulk insert each chunk while skipping existing `external_report_id`;
  - `record_counts` after each chunk;
  - finish at `applied` (or `rejected`, with the problems listed).
- [ ] **Same pattern for templates and for the mining steps** that scan the corpus. Optionally queue `derive-map` → `lexicon-mine` → … automatically once the import is applied.
- [ ] **Admin batch page:** poll `GET /batches/{id}` for status and counts, and list rejected rows.
- [ ] **Readiness and mining ignore batches that aren't `applied` yet**, because chunked commits leave a half-loaded batch visible while the job runs.
- [ ] **Keep the roster upload and single approve/verify/resolve actions synchronous** (they're small, and instant feedback matters).
- [ ] **Tests:**
  - crash after chunk N, then retry: no duplicates;
  - double submit: deduplicated to one job;
  - a malformed file is refused in the request.

## Onboarding: other gaps found 2026-10-13

- [ ] **Enforce `is_deidentified`** where outbound LLM calls are made (`DeidentificationRequired` is never raised).
- [ ] **Move corpus and mining batches out of `parsing`** to `applied`.
- [ ] **Lock the batch row in `apply_templates`** (`SELECT … FOR UPDATE`), and make `ix_template_version_current` unique on `(tenant_id, template_id) WHERE is_current`.
- [ ] **Make sharded registration recoverable:** idempotent `_provision`, plus a sweeper for tenants stuck in `provisioning`.
- [ ] **Offboarding: export, then purge** the lab's data.
- [ ] **Drop or wire up** `corpus_report.radiologist_id` and `corpus_report.embedding`.
- [x] **Readiness thresholds** (done 2026-10-13): gold bundles per template replace the flat 200 verified mappings and the flat 40 frozen items; an accuracy check reads the latest gate run on the frozen acceptance set; the baseline must come from an audit of enough graded reports.
- [ ] **Require every RED starting rule to be approved or waived** when alerts are on, instead of at least one rule; needs a waived state on `critical_finding_rule`.
- [ ] **Run the acceptance accuracy from the panel.** Today the run is recorded with `python -m radreport.eval.gate_run ... --eval-set <acceptance set>`; a button on step 8 would remove the shell step.
- [ ] **Define autonomy classes from the panel.** Step 7 lists classes but they are still created through `POST /admin/api/labs/{id}/autonomy-classes`.
- [ ] **Teach `devtools/demo_lab.py` the new steps** (gold bundles ZIP, baseline audit), so the demo lab can reach pilot.
