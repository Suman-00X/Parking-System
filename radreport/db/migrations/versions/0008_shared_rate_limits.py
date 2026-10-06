"""Adds a request counter every worker shares, for the rate limits that must hold across processes.

Order: upgrade creates the counter table and lets the app role use it; downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_APP_ROLE = "radreport_app"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def upgrade() -> None:
    """Guarded, because revision 0001 builds from the live models."""
    if not _has_table("rate_limit_counter"):
        op.create_table("rate_limit_counter", sa.Column("limit_id", sa.Text(), nullable=False), sa.Column("who", sa.Text(), nullable=False), sa.Column("window_start", sa.DateTime(timezone=True), nullable=False), sa.Column("hits", sa.Integer(), nullable=False, server_default=sa.text("0")), sa.PrimaryKeyConstraint("limit_id", "who", "window_start", name="pk_rate_limit_counter"))
        op.create_index("ix_rate_limit_counter_window", "rate_limit_counter", ["window_start"])
    # No tenant column and no RLS: it counts callers before anyone knows which lab they belong to.
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limit_counter TO {_APP_ROLE}")


def downgrade() -> None:
    if _has_table("rate_limit_counter"):
        op.drop_table("rate_limit_counter")
