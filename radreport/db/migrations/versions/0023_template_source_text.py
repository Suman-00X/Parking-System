"""Keeps the text of each uploaded template on its import candidate, for training the template model.

Order: upgrade adds template_import_candidate.source_text; downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    return any(c["name"] == "source_text" for c in sa.inspect(op.get_bind()).get_columns("template_import_candidate"))


def upgrade() -> None:
    if not _has_column():
        op.add_column("template_import_candidate", sa.Column("source_text", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column("template_import_candidate", "source_text")
