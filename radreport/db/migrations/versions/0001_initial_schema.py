"""Creates the whole schema, generated from the model definitions rather than hand-written table by table.

Order: upgrade builds every table; downgrade drops them.
"""

from __future__ import annotations

from alembic import op

from radreport.db.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # gen_random_uuid() for the uuid PK default; vector for pgvector columns
    # (: Postgres 16 + pgvector); citext for case-insensitive email.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
