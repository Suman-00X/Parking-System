"""Lets a template keep its spoken code across versions, by scoping the uniqueness rule to the current version only.

Order: upgrade swaps the old constraint for the narrower index; downgrade restores it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_OLD_CONSTRAINT = "uq_template_version_tenant_id_spoken_study_code"
_NEW_INDEX = "uq_template_version_current_spoken_code"


def upgrade() -> None:
    op.drop_constraint(_OLD_CONSTRAINT, "template_version", type_="unique")
    op.create_index(
        _NEW_INDEX,
        "template_version",
        ["tenant_id", "spoken_study_code"],
        unique=True,
        # `sa.text`, not `op.inline_literal`: the latter renders a quoted
        # string literal, which Postgres rejects as a boolean predicate.
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    """Reversible only while no template has two versions."""
    op.drop_index(_NEW_INDEX, table_name="template_version")
    op.create_unique_constraint(_OLD_CONSTRAINT, "template_version", ["tenant_id", "spoken_study_code"])
