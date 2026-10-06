"""Adds admin logins and makes the model configuration writable from the admin panel instead of seed scripts only.

Order: upgrade adds the admin and model-assignment tables and widens the step names;
downgrade reverses both. Every step checks the database first, because revision 0001 builds from
the live models and may already have produced the end state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from radreport.core.types import TaskKey

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_TASK_KEY_TABLES = ("task_model_assignment", "task_model_assignment_log")
_OLD_TASK_KEYS = ("extraction", "self_correction", "verification", "routing_pick", "utterance_classification", "routing_shortlist", "roundtrip_check", "compose")


def _task_key_check(values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"task_key IN ({rendered})"


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_index(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :name"), {"name": name}).first())


def _has_constraint(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}).first())


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def _replace_task_key_check(values: tuple[str, ...]) -> None:
    for table in _TASK_KEY_TABLES:
        # The full name, wrapped in op.f so the naming convention does not prefix it a second time.
        name = f"ck_{table}_task_key_valid"
        if _has_constraint(name):
            op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, _task_key_check(values))


def upgrade() -> None:
    """Guarded throughout: on a database built by 0001 from today's models, most steps are no-ops."""
    for table, column in (("platform_user", "password_hash"), ("platform_user", "last_login_at"), ("model_provider", "api_key_env_var")):
        if not _has_column(table, column):
            kind = sa.DateTime(timezone=True) if column == "last_login_at" else sa.Text()
            op.add_column(table, sa.Column(column, kind, nullable=True))

    if not _has_table("admin_session"):
        op.create_table(
            "admin_session",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("platform_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("user_agent", sa.Text(), nullable=True),
            sa.Column("ip_address", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["platform_user_id"], ["platform_user.id"], ondelete="CASCADE"),
        )
    if not _has_index("ix_admin_session_token"):
        op.create_index("ix_admin_session_token", "admin_session", ["token_hash"], unique=True)
    if not _has_index("ix_admin_session_user"):
        op.create_index("ix_admin_session_user", "admin_session", ["platform_user_id", "expires_at"])

    # `admin_session` carries no `tenant_id` and takes no RLS policy: it is a platform-realm table like `platform_user`.
    if _has_role("radreport_app"):
        op.execute("GRANT SELECT, INSERT, UPDATE ON admin_session TO radreport_app")

    _replace_task_key_check(TaskKey.values())


def downgrade() -> None:
    """Reversible only while no ASR assignment exists."""
    _replace_task_key_check(_OLD_TASK_KEYS)

    if _has_table("admin_session"):
        op.drop_table("admin_session")
    for table, column in (("model_provider", "api_key_env_var"), ("platform_user", "last_login_at"), ("platform_user", "password_hash")):
        if _has_column(table, column):
            op.drop_column(table, column)
