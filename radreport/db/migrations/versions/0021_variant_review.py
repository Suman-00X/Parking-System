"""Adds confidence and review status to heard variants, so only confident matches are used unreviewed.

Order: upgrade adds confidence, review_status (existing variants count as approved), threshold_arm,
decided_by and decided_at, with a check on the status values and an index for the review queue;
downgrade removes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

COLUMNS = (("confidence", sa.Numeric(5, 4), {}), ("review_status", sa.String(16), {"nullable": False, "server_default": "approved"}), ("threshold_arm", sa.String(1), {}), ("decided_by", PGUUID(as_uuid=True), {}), ("decided_at", sa.DateTime(timezone=True), {}))


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("lexicon_surface_variant")}


def _has_constraint(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).first())


def upgrade() -> None:
    """Guarded: 0001 builds these from the model on a fresh database."""
    present = _columns()
    for name, kind, extra in COLUMNS:
        if name not in present:
            op.add_column("lexicon_surface_variant", sa.Column(name, kind, nullable=extra.get("nullable", True), server_default=extra.get("server_default")))
    if not _has_constraint("ck_lexicon_surface_variant_review_status_valid"):
        op.create_check_constraint(op.f("ck_lexicon_surface_variant_review_status_valid"), "lexicon_surface_variant", "review_status IN ('auto_approved', 'approved', 'pending', 'rejected')")
    op.execute("CREATE INDEX IF NOT EXISTS ix_surface_variant_review ON lexicon_surface_variant (tenant_id, review_status)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_surface_variant_review")
    if _has_constraint("ck_lexicon_surface_variant_review_status_valid"):
        op.drop_constraint(op.f("ck_lexicon_surface_variant_review_status_valid"), "lexicon_surface_variant")
    present = _columns()
    for name, _kind, _extra in reversed(COLUMNS):
        if name in present:
            op.drop_column("lexicon_surface_variant", name)
