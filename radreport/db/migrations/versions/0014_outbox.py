"""Adds the transactional outbox: domain events written with each change, relayed once, applied once per consumer.

Order: upgrade creates outbox_event (lab-isolated) and consumed_event, plus two functions owned by the
BYPASSRLS view-owner role so the relay can see every lab's unsent events (claim_outbox, with FOR
UPDATE SKIP LOCKED) and mark them sent (mark_outbox); downgrade drops them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_APP_ROLE = "radreport_app"
_VIEW_OWNER = "radreport_views"

CLAIM = """
CREATE OR REPLACE FUNCTION claim_outbox(p_limit integer)
RETURNS SETOF outbox_event LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT * FROM outbox_event WHERE published_at IS NULL ORDER BY id LIMIT p_limit FOR UPDATE SKIP LOCKED
$$;
"""

MARK = """
CREATE OR REPLACE FUNCTION mark_outbox(p_ids bigint[], p_error text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE touched integer;
BEGIN
    IF p_error IS NULL THEN
        UPDATE outbox_event SET published_at = now(), attempts = attempts + 1, last_error = NULL WHERE id = ANY(p_ids);
    ELSE
        UPDATE outbox_event SET attempts = attempts + 1, last_error = p_error WHERE id = ANY(p_ids);
    END IF;
    GET DIAGNOSTICS touched = ROW_COUNT;
    RETURN touched;
END $$;
"""


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_policy(table: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = 'tenant_isolation'"), {"t": table}).first())


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def upgrade() -> None:
    """Each step guarded: on a fresh database 0001 built the tables and 0002 the policy."""
    if not _has_table("outbox_event"):
        op.create_table(
            "outbox_event",
            sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
            sa.Column("tenant_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=True),
            sa.Column("event_id", PGUUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("topic", sa.Text(), nullable=False),
            sa.Column("key", sa.Text(), nullable=False),
            sa.Column("payload", JSONB(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_error", sa.Text(), nullable=True),
        )
        op.create_index("ix_outbox_event_unsent", "outbox_event", ["id"], postgresql_where=sa.text("published_at IS NULL"))
        op.create_index("ix_outbox_event_tenant", "outbox_event", ["tenant_id", "created_at"])
    if not _has_table("consumed_event"):
        op.create_table("consumed_event", sa.Column("consumer", sa.Text(), nullable=False), sa.Column("event_id", PGUUID(as_uuid=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")), sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_consumed_event")))
    if not _has_policy("outbox_event"):
        op.execute("ALTER TABLE outbox_event ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE outbox_event FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON outbox_event USING (tenant_id IS NULL OR tenant_id = {_TENANT}) WITH CHECK ((tenant_id IS NULL AND {_TENANT} IS NULL) OR tenant_id = {_TENANT})")
    op.execute(CLAIM)
    op.execute(MARK)
    op.execute("REVOKE ALL ON FUNCTION claim_outbox(integer) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION mark_outbox(bigint[], text) FROM PUBLIC")
    if _has_role(_VIEW_OWNER):
        op.execute(f"GRANT SELECT, UPDATE ON outbox_event TO {_VIEW_OWNER}")
        op.execute(f"ALTER FUNCTION claim_outbox(integer) OWNER TO {_VIEW_OWNER}")
        op.execute(f"ALTER FUNCTION mark_outbox(bigint[], text) OWNER TO {_VIEW_OWNER}")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT, INSERT ON outbox_event TO {_APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT, DELETE ON consumed_event TO {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION claim_outbox(integer) TO {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION mark_outbox(bigint[], text) TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claim_outbox(integer)")
    op.execute("DROP FUNCTION IF EXISTS mark_outbox(bigint[], text)")
    for table in ("consumed_event", "outbox_event"):
        if _has_table(table):
            op.drop_table(table)
