"""Adds work_backlog(), the counts the metrics endpoint reports for the job queue and the outbox.

Order: upgrade creates work_backlog, owned by the BYPASSRLS view-owner role so a scrape counts every
lab's jobs and events, returning totals per kind and state only and never a row; downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_APP_ROLE = "radreport_app"
_VIEW_OWNER = "radreport_views"

BACKLOG = """
CREATE OR REPLACE FUNCTION work_backlog()
RETURNS TABLE (source text, kind text, state text, items bigint, oldest_seconds double precision)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT 'job', j.kind, j.status, count(*), COALESCE(EXTRACT(EPOCH FROM now() - min(j.run_at)), 0)
      FROM job j WHERE j.status IN ('queued', 'running', 'dead') GROUP BY j.kind, j.status
    UNION ALL
    SELECT 'outbox', o.topic, 'unsent', count(*), COALESCE(EXTRACT(EPOCH FROM now() - min(o.created_at)), 0)
      FROM outbox_event o WHERE o.published_at IS NULL GROUP BY o.topic
$$;
"""


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def upgrade() -> None:
    op.execute(BACKLOG)
    op.execute("REVOKE ALL ON FUNCTION work_backlog() FROM PUBLIC")
    if _has_role(_VIEW_OWNER):
        op.execute(f"ALTER FUNCTION work_backlog() OWNER TO {_VIEW_OWNER}")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT EXECUTE ON FUNCTION work_backlog() TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS work_backlog()")
