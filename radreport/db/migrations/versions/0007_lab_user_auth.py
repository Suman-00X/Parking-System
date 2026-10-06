"""Adds lab-user sign-in: a password on each staff account and a per-lab table of refresh tokens.

Order: upgrade adds the password columns, creates the refresh-token table, and gives it the same
row-level isolation every lab table has; downgrade removes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_APP_ROLE = "radreport_app"


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_policy(table: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = 'tenant_isolation'"), {"t": table}).first())


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def upgrade() -> None:
    """Each step is guarded, because revision 0001 builds from the live models."""
    if not _has_column("app_user", "password_hash"):
        op.add_column("app_user", sa.Column("password_hash", sa.Text(), nullable=True))
    if not _has_column("app_user", "last_login_at"):
        op.add_column("app_user", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    if not _has_table("lab_refresh_token"):
        op.create_table(
            "lab_refresh_token",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("app_user_id", PGUUID(as_uuid=True), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False),
            sa.Column("family_id", PGUUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("user_agent", sa.Text(), nullable=True),
            sa.Column("ip_address", sa.Text(), nullable=True),
            sa.UniqueConstraint("id", "tenant_id", name="uq_lab_refresh_token_id_tenant_id"),
            sa.ForeignKeyConstraint(["app_user_id", "tenant_id"], ["app_user.id", "app_user.tenant_id"], ondelete="CASCADE", name="fk_app_user_id_tenant"),
        )
        op.create_index("ix_lab_refresh_token_hash", "lab_refresh_token", ["token_hash"], unique=True)
        op.create_index("ix_lab_refresh_token_user", "lab_refresh_token", ["tenant_id", "app_user_id", "expires_at"])
        op.create_index("ix_lab_refresh_token_family", "lab_refresh_token", ["tenant_id", "family_id"])

    if not _has_policy("lab_refresh_token"):
        op.execute("ALTER TABLE lab_refresh_token ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE lab_refresh_token FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON lab_refresh_token USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON lab_refresh_token TO {_APP_ROLE}")


def downgrade() -> None:
    """Signs every lab user out and removes their passwords."""
    if _has_table("lab_refresh_token"):
        op.drop_table("lab_refresh_token")
    for column in ("last_login_at", "password_hash"):
        if _has_column("app_user", column):
            op.drop_column("app_user", column)
