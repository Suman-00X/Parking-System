"""Adds admin logins and makes the model configuration writable from the admin panel instead of seed scripts only.

Order: upgrade adds the admin and model-assignment tables and widens the step names;
downgrade reverses both.
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


def upgrade() -> None:
    op.add_column("platform_user", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column("platform_user", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("model_provider", sa.Column("api_key_env_var", sa.Text(), nullable=True))

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
    op.create_index("ix_admin_session_token", "admin_session", ["token_hash"], unique=True)
    op.create_index("ix_admin_session_user", "admin_session", ["platform_user_id", "expires_at"])

    # `admin_session` carries no `tenant_id` and takes no RLS policy: it is a platform-realm table like `platform_user`, and `core.tenancy` lists it on the exception list so the build-time check agrees.
    op.execute("GRANT SELECT, INSERT, UPDATE ON admin_session TO radreport_app")

    for table in _TASK_KEY_TABLES:
        # Both calls take the **bare** name: alembic applies the metadata naming convention (`ck_%(table_name)s_%(constraint_name)s`) on drop as well as on create, so passing the already-prefixed name gets it prefixed twice and truncated to a hash that matches nothing.
        op.drop_constraint("task_key_valid", table, type_="check")
        op.create_check_constraint("task_key_valid", table, _task_key_check(TaskKey.values()))


def downgrade() -> None:
    """Reversible only while no ASR assignment exists."""
    for table in _TASK_KEY_TABLES:
        op.drop_constraint("task_key_valid", table, type_="check")
        op.create_check_constraint("task_key_valid", table, _task_key_check(_OLD_TASK_KEYS))

    op.drop_index("ix_admin_session_user", table_name="admin_session")
    op.drop_index("ix_admin_session_token", table_name="admin_session")
    op.drop_table("admin_session")
    op.drop_column("model_provider", "api_key_env_var")
    op.drop_column("platform_user", "last_login_at")
    op.drop_column("platform_user", "password_hash")
