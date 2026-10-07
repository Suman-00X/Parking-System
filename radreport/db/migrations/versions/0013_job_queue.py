"""Adds the job queue: a job table under row-level security, and claim_jobs(), which hands out work with FOR UPDATE SKIP LOCKED.

Order: upgrade creates the table and its indexes, the lab-isolation policy, and two functions owned
by the BYPASSRLS view-owner role (claim_jobs to lease ready work across every lab, reap_jobs to
bury leases that ran out on their last attempt); downgrade drops them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_APP_ROLE = "radreport_app"
_VIEW_OWNER = "radreport_views"

CLAIM = """
CREATE OR REPLACE FUNCTION claim_jobs(p_worker text, p_kinds text[], p_limit integer, p_visibility_seconds integer)
RETURNS SETOF job LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    -- A lease that ran out on the last allowed attempt is a poison job: bury it rather than hand it out again.
    UPDATE job SET status = 'dead', finished_at = now(), locked_by = NULL, locked_until = NULL,
           last_error = COALESCE(last_error || ' | ', '') || 'lease expired on the final attempt'
     WHERE status = 'running' AND locked_until < now() AND attempts >= max_attempts AND kind = ANY(p_kinds);
    RETURN QUERY
    UPDATE job j SET status = 'running', locked_by = p_worker, locked_until = now() + make_interval(secs => p_visibility_seconds),
           attempts = j.attempts + 1, started_at = now()
     WHERE j.id IN (
           SELECT id FROM job
            WHERE kind = ANY(p_kinds)
              AND ((status = 'queued' AND run_at <= now()) OR (status = 'running' AND locked_until < now() AND attempts < max_attempts))
            ORDER BY priority DESC, run_at, created_at
            LIMIT p_limit
            FOR UPDATE SKIP LOCKED)
    RETURNING j.*;
END $$;
"""

REAP = """
CREATE OR REPLACE FUNCTION reap_jobs() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE buried integer;
BEGIN
    UPDATE job SET status = 'dead', finished_at = now(), locked_by = NULL, locked_until = NULL,
           last_error = COALESCE(last_error || ' | ', '') || 'lease expired on the final attempt'
     WHERE status = 'running' AND locked_until < now() AND attempts >= max_attempts;
    GET DIAGNOSTICS buried = ROW_COUNT;
    RETURN buried;
END $$;
"""


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_policy(table: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = 'tenant_isolation'"), {"t": table}).first())


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def upgrade() -> None:
    """Each step guarded: on a fresh database 0001 built the table and 0002 its policy."""
    if not _has_table("job"):
        op.create_table(
            "job",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", PGUUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=True),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("priority", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
            sa.Column("locked_by", sa.Text(), nullable=True),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dedupe_key", sa.Text(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("result", JSONB(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
            sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'dead')", name=op.f("ck_job_status_valid")),
        )
        op.create_index("ix_job_ready", "job", ["kind", "priority", "run_at"], postgresql_where=sa.text("status = 'queued'"))
        op.create_index("ix_job_leased", "job", ["locked_until"], postgresql_where=sa.text("status = 'running'"))
        op.create_index("ix_job_tenant", "job", ["tenant_id", "created_at"])
        op.execute("CREATE UNIQUE INDEX uq_job_dedupe ON job (COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), kind, dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running')")
    if not _has_policy("job"):
        op.execute("ALTER TABLE job ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE job FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON job USING (tenant_id IS NULL OR tenant_id = {_TENANT}) WITH CHECK ((tenant_id IS NULL AND {_TENANT} IS NULL) OR tenant_id = {_TENANT})")
    op.execute(CLAIM)
    op.execute(REAP)
    op.execute("REVOKE ALL ON FUNCTION claim_jobs(text, text[], integer, integer) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION reap_jobs() FROM PUBLIC")
    if _has_role(_VIEW_OWNER):
        # The function runs as this role so a worker can lease any lab's work; the lab's own rows are then touched only from a session bound to it.
        op.execute(f"GRANT SELECT, UPDATE ON job TO {_VIEW_OWNER}")
        op.execute(f"ALTER FUNCTION claim_jobs(text, text[], integer, integer) OWNER TO {_VIEW_OWNER}")
        op.execute(f"ALTER FUNCTION reap_jobs() OWNER TO {_VIEW_OWNER}")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON job TO {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION claim_jobs(text, text[], integer, integer) TO {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION reap_jobs() TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claim_jobs(text, text[], integer, integer)")
    op.execute("DROP FUNCTION IF EXISTS reap_jobs()")
    if _has_table("job"):
        op.drop_table("job")
