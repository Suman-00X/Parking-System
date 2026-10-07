"""Adds llm_response_cache: stored model replies keyed by lab and an exact request fingerprint.

Order: upgrade creates the table with its unique key and expiry index, and the lab-isolation policy
every lab table has; downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_APP_ROLE = "radreport_app"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_policy(table: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = 'tenant_isolation'"), {"t": table}).first())


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def upgrade() -> None:
    """Guarded: on a fresh database 0001 built the table and 0002 its policy."""
    if not _has_table("llm_response_cache"):
        op.create_table(
            "llm_response_cache",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("cache_key", sa.Text(), nullable=False),
            sa.Column("model_id", sa.Text(), nullable=False),
            sa.Column("task_key", sa.Text(), nullable=True),
            sa.Column("response", JSONB(), nullable=False),
            sa.Column("hits", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("id", "tenant_id", name="uq_llm_response_cache_id_tenant_id"),
            sa.UniqueConstraint("tenant_id", "cache_key", name="uq_llm_response_cache_tenant_id_cache_key"),
        )
        op.create_index("ix_llm_response_cache_expiry", "llm_response_cache", ["expires_at"])
    if not _has_policy("llm_response_cache"):
        op.execute("ALTER TABLE llm_response_cache ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE llm_response_cache FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON llm_response_cache USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON llm_response_cache TO {_APP_ROLE}")


def downgrade() -> None:
    if _has_table("llm_response_cache"):
        op.drop_table("llm_response_cache")
