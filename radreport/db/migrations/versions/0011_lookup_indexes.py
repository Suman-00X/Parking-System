"""Adds the indexes the query report found missing: foreign keys the hot paths join on, and the cost and per-task metric reads.

Order: upgrade creates each index if it is absent; downgrade drops them.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

#: name -> the statement body after `ON`. Kept identical to the models so a fresh database (built by 0001) already has them.
INDEXES: dict[str, str] = {
    "ix_pipeline_run_recording": "pipeline_run (tenant_id, recording_id)",
    "ix_pipeline_run_cost": "pipeline_run (tenant_id, created_at) INCLUDE (total_cost_usd) WHERE NOT is_shadow",
    "ix_stage_execution_task": "stage_execution (tenant_id, task_key, created_at)",
    "ix_report_draft_recording": "report_draft (tenant_id, recording_id)",
    "ix_recording_radiologist": "recording (tenant_id, radiologist_id)",
    "ix_critical_finding_alert_recording": "critical_finding_alert (tenant_id, recording_id)",
    "ix_final_report_draft": "final_report (tenant_id, report_draft_id)",
    "ix_provenance_span_utterance": "provenance_span (tenant_id, utterance_id)",
    "ix_edit_event_revision": "edit_event (tenant_id, report_revision_id)",
    "ix_template_import_candidate_batch": "template_import_candidate (tenant_id, import_batch_id)",
    "ix_corpus_report_batch": "corpus_report (tenant_id, import_batch_id)",
}


def upgrade() -> None:
    """IF NOT EXISTS is the guard: 0001 builds these from the models on a fresh database."""
    for name, target in INDEXES.items():
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")


def downgrade() -> None:
    for name in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
