"""Lets a template keep its spoken code across versions, by scoping the uniqueness rule to the current version only.

Order: upgrade swaps the old constraint for the narrower index; downgrade restores it. Every
step checks the database first, because revision 0001 builds from the live models and may
already have produced the end state.
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


def _has_constraint(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}).first())


def _has_index(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :name"), {"name": name}).first())


def upgrade() -> None:
    """Guarded: on a database built by 0001 from today's models, there is nothing to do."""
    if _has_constraint(_OLD_CONSTRAINT):
        # op.f: the name is already final, so the naming convention must not prefix it again.
        op.drop_constraint(op.f(_OLD_CONSTRAINT), "template_version", type_="unique")
    if _has_index(_NEW_INDEX):
        return
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
    if _has_index(_NEW_INDEX):
        op.drop_index(_NEW_INDEX, table_name="template_version")
    if not _has_constraint(_OLD_CONSTRAINT):
        op.create_unique_constraint(op.f(_OLD_CONSTRAINT), "template_version", ["tenant_id", "spoken_study_code"])
