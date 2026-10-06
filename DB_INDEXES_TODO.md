# Database indexes: findings and what is still open

How this was produced, and how to redo it against a real workload:

```bash
make pg-observe                        # once: slow-query log + pg_stat_statements, then restart Postgres
python -m radreport.devtools.query_report --url "$OWNER_URL" --reset
# ... let traffic run (production: a full business day; here: the full test suite) ...
python -m radreport.devtools.query_report --url "$OWNER_URL" --top 20 > report.md
```

The report lists the 20 statements with the most total time, with the generic plan of each
SELECT (`EXPLAIN (GENERIC_PLAN)`, Postgres 16, so nothing is executed), every foreign key
with no index behind it, the tables read mostly by sequential scan, and the indexes nothing
has scanned.

**Workload used for the findings below:** the full test suite (719 tests) against
`radreport_test` on 2026-10-07. There is no production database yet, so this is a stand-in:
it exercises every route and every write path, but its row counts are tiny, so sequential
scans on small tables in the report are the planner's correct choice and are not findings.
Rerun the report on production once it exists and compare.

## Done (migration 0011)

| Index | Why |
|---|---|
| `pipeline_run (tenant_id, recording_id)` | Every "runs for this recording" read and the cascade from `recording`. Was a sequential scan of `pipeline_run`. |
| `pipeline_run (tenant_id, created_at) INCLUDE (total_cost_usd) WHERE NOT is_shadow` | The cost dashboard and the metering rollup: an index-only scan per lab and date range. `ix_pipeline_run_status` leads with `status`, so it could not serve a date range across statuses. |
| `stage_execution (tenant_id, task_key, created_at)` | Per-task cost and latency. `ix_stage_execution_cost` leads with `stage_name`, which is not the task. |
| `report_draft (tenant_id, recording_id)` | Review queue joins and the cascade from `recording`. |
| `recording (tenant_id, radiologist_id)` | Per-radiologist reads (speaker balance, adaptation corpus) and the `RESTRICT` check when a profile is removed. |
| `critical_finding_alert (tenant_id, recording_id)` | The queue's alerts-by-recording read. |
| `final_report (tenant_id, report_draft_id)` | Signing checks whether a draft already has a report. |
| `provenance_span (tenant_id, utterance_id)` | The `SET NULL` from `transcript_utterance`. |
| `edit_event (tenant_id, report_revision_id)` | The `CASCADE` from `report_revision`; partitioned, so the index is created on every partition. |
| `template_import_candidate (tenant_id, import_batch_id)` | Onboarding batch pages list candidates by batch. |
| `corpus_report (tenant_id, import_batch_id)` | Batch revert and the batch status endpoint. |

## Checked and not needed

- **`study.patient_id` without a `tenant_id` prefix.** Every read of `study` carries the RLS
  predicate `tenant_id = current lab`, so a `patient_id`-only index would be used with the
  tenant filter applied afterwards; `ix_study_patient_datetime (tenant_id, patient_id,
  study_datetime)` already serves both equality columns. Nothing in the code reads studies
  across labs.
- **`report_revision`, `verbatim_transcript`, `corpus_report_template_map`.** The report
  lists their `(parent_id, tenant_id)` keys because no index leads with exactly those two
  columns, but each has a unique constraint leading with `parent_id`, which serves the
  lookup. Covered.

## Still open

- **Deleting a lab.** The heaviest statement in the test workload is `DELETE FROM tenant`
  (0.45 ms mean on empty tables): the check of every child foreign key scans each child
  table that has no index leading with `tenant_id` alone. About 25 tables are in that list
  (the report's "Foreign keys with no supporting index" section, rows naming
  `fk_<table>_tenant_id`). Offboarding is rare and purges children first, so this is not
  worth the write cost of 25 indexes today. Revisit if offboarding becomes routine.
- **Low-cardinality foreign keys** (`approved_by`, `decided_by`, `acknowledged_by`,
  `signed_by`, `actor_id`): only read when a user row is deleted, which the product never
  does (users are deactivated). Left unindexed on purpose.
- **Production rerun.** Replace the stand-in workload with `pg_stat_statements` from
  production and EXPLAIN ANALYZE the top 20 there (on a replica: ANALYZE executes the
  statement).
