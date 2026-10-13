"""Adds baseline_audit_grade: each signed report an independent grader re-read to measure an autonomy class's baseline error rate.

Order: upgrade creates the lab table with its keys and the lab-isolation policy; downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_TABLE = "baseline_audit_grade"
_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_APP_ROLE = "radreport_app"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_policy(table: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = 'tenant_isolation'"), {"t": table}).first())


def _isolate(table: str) -> None:
    if not _has_policy(table):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})")
    if op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": _APP_ROLE}).first():
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_APP_ROLE}")


def upgrade() -> None:
    """Guarded: on a fresh database 0001 built the table and 0002 its policy."""
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", PGUUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
            sa.Column("autonomy_class_id", PGUUID(as_uuid=True), nullable=False),
            sa.Column("corpus_report_id", PGUUID(as_uuid=True), nullable=False),
            sa.Column("severity_grade", sa.String(4), nullable=False),
            sa.Column("is_cse", sa.Boolean(), nullable=False),
            sa.Column("grader", sa.Text(), nullable=False),
            sa.Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_baseline_audit_grade")),
            sa.ForeignKeyConstraint(["autonomy_class_id", "tenant_id"], ["autonomy_class.id", "autonomy_class.tenant_id"], ondelete="CASCADE", name=op.f("fk_autonomy_class_id_tenant")),
            sa.ForeignKeyConstraint(["corpus_report_id", "tenant_id"], ["corpus_report.id", "corpus_report.tenant_id"], ondelete="CASCADE", name=op.f("fk_corpus_report_id_tenant")),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT", name=op.f("fk_baseline_audit_grade_tenant_id")),
            sa.UniqueConstraint("autonomy_class_id", "corpus_report_id", name=op.f("uq_baseline_audit_grade_autonomy_class_id_corpus_report_id")),
            sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_baseline_audit_grade_id_tenant_id")),
            sa.CheckConstraint("severity_grade IN ('G0', 'G1', 'G2', 'G3', 'G4')", name=op.f("ck_baseline_audit_grade_severity_grade_valid")),
        )
        op.create_index("ix_baseline_audit_grade_class", _TABLE, ["tenant_id", "autonomy_class_id"])
    _isolate(_TABLE)


def downgrade() -> None:
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
