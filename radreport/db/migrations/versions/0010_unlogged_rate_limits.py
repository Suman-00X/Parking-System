"""Makes the shared rate-limit counter unlogged, now that every request counts against it.

Order: upgrade switches the table to UNLOGGED; downgrade switches it back.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _persistence() -> str | None:
    """`u` for unlogged, `p` for permanent, None if the table is absent."""
    return op.get_bind().execute(sa.text("SELECT relpersistence FROM pg_class WHERE relname = 'rate_limit_counter' AND relkind = 'r'")).scalar()


def upgrade() -> None:
    """Guarded: 0001 already creates it unlogged on a fresh database."""
    if _persistence() == "p":
        # A crash loses at most a minute of counts; the write-ahead log would cost every request.
        op.execute("ALTER TABLE rate_limit_counter SET UNLOGGED")


def downgrade() -> None:
    if _persistence() == "u":
        op.execute("ALTER TABLE rate_limit_counter SET LOGGED")
