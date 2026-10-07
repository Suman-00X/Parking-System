"""Adds system_config: operational thresholds ops can change without a release, platform-wide or per lab.

Order: upgrade creates the table, its two partial unique indexes and its row-level policy (NULL
tenant = platform-wide, visible to every lab, written only from a session bound to no lab);
downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0012"
down_revision = "0011"
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
    """Each step guarded: on a fresh database 0001 and 0002 have already built all of it."""
    if not _has_table("system_config"):
        op.create_table("system_config", sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")), sa.Column("tenant_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=True), sa.Column("key", sa.Text(), nullable=False), sa.Column("value", JSONB(), nullable=False), sa.Column("updated_by", PGUUID(as_uuid=True), nullable=True), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")), sa.PrimaryKeyConstraint("id", name=op.f("pk_system_config")))
        op.create_index("uq_system_config_global_key", "system_config", ["key"], unique=True, postgresql_where=sa.text("tenant_id IS NULL"))
        op.create_index("uq_system_config_tenant_key", "system_config", ["tenant_id", "key"], unique=True, postgresql_where=sa.text("tenant_id IS NOT NULL"))
    if not _has_policy("system_config"):
        op.execute("ALTER TABLE system_config ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE system_config FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON system_config USING (tenant_id IS NULL OR tenant_id = {_TENANT}) WITH CHECK ((tenant_id IS NULL AND {_TENANT} IS NULL) OR tenant_id = {_TENANT})")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON system_config TO {_APP_ROLE}")


def downgrade() -> None:
    if _has_table("system_config"):
        op.drop_table("system_config")
