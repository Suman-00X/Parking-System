"""Records which product admin submitted an import batch run from the admin panel.

Order: upgrade adds the nullable column and its foreign key; downgrade removes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_FK = "fk_import_batch_submitted_by_platform_user_id"


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def _has_constraint(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}).first())


def upgrade() -> None:
    """Guarded, because revision 0001 builds from the live models."""
    if not _has_column("import_batch", "submitted_by_platform_user_id"):
        op.add_column("import_batch", sa.Column("submitted_by_platform_user_id", PGUUID(as_uuid=True), nullable=True))
    if not _has_constraint(_FK):
        op.create_foreign_key(op.f(_FK), "import_batch", "platform_user", ["submitted_by_platform_user_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    if _has_constraint(_FK):
        op.drop_constraint(op.f(_FK), "import_batch", type_="foreignkey")
    if _has_column("import_batch", "submitted_by_platform_user_id"):
        op.drop_column("import_batch", "submitted_by_platform_user_id")
