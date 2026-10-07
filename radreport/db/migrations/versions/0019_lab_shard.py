"""Adds lab_shard: labs pinned to a database shard regardless of the hash ring.

Order: upgrade creates the table in whichever database it runs on (only the directory database's
copy is read); downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Guarded: on a fresh database 0001 already built it."""
    if not sa.inspect(op.get_bind()).has_table("lab_shard"):
        op.create_table("lab_shard", sa.Column("lab_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True), sa.Column("shard_name", sa.Text(), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")))
    if op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'radreport_app'")).first():
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON lab_shard TO radreport_app")


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("lab_shard"):
        op.drop_table("lab_shard")
