"""Adds the schema that lets an approved class of reports be filed without a human reviewer.

Order: upgrade adds the release path and its check constraint; downgrade removes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from radreport.core.types import PathType

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

#: The two values `path_type` held before this revision.
_OLD_PATH_TYPES = ("transcriptionist_reviewed", "radiologist_only")

_AUTONOMOUS_CHECK = "(path_type = 'autonomous' AND final_revision_id IS NULL AND autonomy_class_id IS NOT NULL) OR (path_type <> 'autonomous' AND (final_revision_id IS NOT NULL OR amends_report_id IS NOT NULL))"


def _path_type_in(values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"path_type IN ({rendered})"


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_constraint(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}).first())


def _has_index(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :name"), {"name": name}).first())


def upgrade() -> None:
    """Each step is guarded, because revision 0001 is not a frozen baseline."""
    if not _has_column("final_report", "autonomy_class_id"):
        op.add_column("final_report", sa.Column("autonomy_class_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))

    # Composite, like every other tenant-scoped FK: a child row must not
    # be able to reference a parent belonging to another lab.
    if not _has_constraint("fk_autonomy_class_id_tenant"):
        op.create_foreign_key("fk_autonomy_class_id_tenant", "final_report", "autonomy_class", ["autonomy_class_id", "tenant_id"], ["id", "tenant_id"], ondelete="RESTRICT")

    # Idempotent as written: dropping NOT NULL from a nullable column is a no-op.
    op.alter_column("final_report", "final_revision_id", nullable=True)

    # Rebuilt unconditionally — the old two-value definition and the new
    # three-value one share a name, so existence does not tell them apart.
    if _has_constraint("ck_final_report_path_type_valid"):
        op.drop_constraint("ck_final_report_path_type_valid", "final_report", type_="check")
    op.create_check_constraint("ck_final_report_path_type_valid", "final_report", sa.text(_path_type_in(PathType.values())))

    # Added after the column, so existing rows — all of which have a revision
    # and a human path_type — already satisfy it and no backfill is needed.
    if not _has_constraint("ck_final_report_autonomous_iff_unreviewed"):
        op.create_check_constraint("ck_final_report_autonomous_iff_unreviewed", "final_report", sa.text(_AUTONOMOUS_CHECK))

    if not _has_index("ix_final_report_path"):
        op.create_index("ix_final_report_path", "final_report", ["tenant_id", "path_type", "signed_at"])


def downgrade() -> None:
    """Reverses cleanly only while no report has been released autonomously."""
    bind = op.get_bind()
    released = bind.execute(sa.text("SELECT count(*) FROM final_report WHERE path_type = 'autonomous'")).scalar_one()
    if released:
        raise RuntimeError(f"{released} report(s) were released autonomously; downgrading would require inventing a review that never happened. Revoke autonomy and keep this revision, or handle those rows deliberately first.")

    if _has_index("ix_final_report_path"):
        op.drop_index("ix_final_report_path", table_name="final_report")
    if _has_constraint("ck_final_report_autonomous_iff_unreviewed"):
        op.drop_constraint("ck_final_report_autonomous_iff_unreviewed", "final_report", type_="check")
    if _has_constraint("ck_final_report_path_type_valid"):
        op.drop_constraint("ck_final_report_path_type_valid", "final_report", type_="check")
    op.create_check_constraint("ck_final_report_path_type_valid", "final_report", sa.text(_path_type_in(_OLD_PATH_TYPES)))
    op.alter_column("final_report", "final_revision_id", nullable=False)
    if _has_constraint("fk_autonomy_class_id_tenant"):
        op.drop_constraint("fk_autonomy_class_id_tenant", "final_report", type_="foreignkey")
    if _has_column("final_report", "autonomy_class_id"):
        op.drop_column("final_report", "autonomy_class_id")
