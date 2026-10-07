"""Adds potential_lexicon_term and lexicon_watch_state: phrases radiologists use that the lexicon lacks, and how far the watcher has read.

Order: upgrade creates both lab tables with their keys and the lab-isolation policy; downgrade drops them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

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
    """Guarded: on a fresh database 0001 built the tables and 0002 their policies."""
    if not _has_table("potential_lexicon_term"):
        op.create_table(
            "potential_lexicon_term",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("surface_text", sa.Text(), nullable=False),
            sa.Column("normalized_text", sa.Text(), nullable=False),
            sa.Column("term_type", sa.String(24), nullable=False),
            sa.Column("frequency", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("contexts", ARRAY(sa.Text()), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_by", PGUUID(as_uuid=True), nullable=True),
            sa.Column("lexicon_set_version_id", PGUUID(as_uuid=True), sa.ForeignKey("lexicon_set.id", ondelete="SET NULL"), nullable=True),
            sa.UniqueConstraint("id", "tenant_id", name="uq_potential_lexicon_term_id_tenant_id"),
            sa.UniqueConstraint("tenant_id", "normalized_text", name="uq_potential_lexicon_term_tenant_id_normalized_text"),
            sa.ForeignKeyConstraint(["decided_by", "tenant_id"], ["app_user.id", "app_user.tenant_id"], ondelete="SET NULL", name="fk_decided_by_tenant"),
            sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name=op.f("ck_potential_lexicon_term_status_valid")),
        )
        op.create_index("ix_potential_lexicon_term_review", "potential_lexicon_term", ["tenant_id", "status", "frequency"])
    if not _has_table("lexicon_watch_state"):
        op.create_table("lexicon_watch_state", sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")), sa.Column("tenant_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False), sa.Column("last_edit_event_at", sa.DateTime(timezone=True), nullable=True), sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True), sa.UniqueConstraint("tenant_id", name="uq_lexicon_watch_state_tenant_id"))
    _isolate("potential_lexicon_term")
    _isolate("lexicon_watch_state")


def downgrade() -> None:
    for table in ("lexicon_watch_state", "potential_lexicon_term"):
        if _has_table(table):
            op.drop_table(table)
