"""Adds the cross-lab cost reads behind the cost dashboard, and a materialized copy of the canonical eval set.

Order: upgrade creates tenant_daily_cost and tenant_stage_cost, date-ranged functions owned by the
BYPASSRLS view-owner role (so they read every lab, use the cost index and prune stage_execution's
monthly partitions), then mv_canonical_eval_set with the unique index a concurrent refresh needs,
and refresh_canonical_eval_set to refresh it; downgrade drops them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_APP_ROLE = "radreport_app"
_VIEW_OWNER = "radreport_views"

DAILY = """
CREATE OR REPLACE FUNCTION tenant_daily_cost(p_from date, p_to date, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, day date, run_count bigint, total_cost_usd numeric, avg_cost_usd numeric, max_cost_usd numeric, failed_runs bigint, budget_hits bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT pr.tenant_id, (pr.created_at AT TIME ZONE 'UTC')::date, count(*), sum(pr.total_cost_usd), avg(pr.total_cost_usd), max(pr.total_cost_usd),
           count(*) FILTER (WHERE pr.status = 'failed'),
           count(*) FILTER (WHERE pr.budget_cap_usd IS NOT NULL AND pr.total_cost_usd >= pr.budget_cap_usd)
      FROM pipeline_run pr
     WHERE NOT pr.is_shadow AND pr.created_at >= p_from AND pr.created_at < p_to + 1 AND (p_tenant IS NULL OR pr.tenant_id = p_tenant)
     GROUP BY 1, 2
$$;
"""

STAGES = """
CREATE OR REPLACE FUNCTION tenant_stage_cost(p_from date, p_to date, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, stage_name text, task_key text, executions bigint, cost_usd numeric, tokens_in bigint, tokens_out bigint, cache_read_tokens bigint, avg_duration_ms numeric, failures bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT se.tenant_id, se.stage_name, se.task_key, count(*), sum(COALESCE(se.cost_usd, 0)), sum(COALESCE(se.tokens_in, 0)), sum(COALESCE(se.tokens_out, 0)), sum(COALESCE(se.cache_read_tokens, 0)), avg(se.duration_ms),
           count(*) FILTER (WHERE se.status = 'failed')
      FROM stage_execution se
     WHERE se.created_at >= p_from AND se.created_at < p_to + 1 AND (p_tenant IS NULL OR se.tenant_id = p_tenant)
     GROUP BY 1, 2, 3
$$;
"""

MATERIALIZED = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_canonical_eval_set AS
SELECT es.id, es.name, es.is_frozen, ei.id AS eval_item_id, ei.source_tenant_id, ei.capture_device_class, ei.audio_quality_bucket
  FROM eval_set es JOIN eval_item ei ON ei.eval_set_id = es.id
 WHERE es.tenant_id IS NULL
"""

REFRESH = """
CREATE OR REPLACE FUNCTION refresh_canonical_eval_set() RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE rows bigint;
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_canonical_eval_set;
    SELECT count(*) INTO rows FROM mv_canonical_eval_set;
    RETURN rows;
END $$;
"""


def _has_role(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def upgrade() -> None:
    op.execute(DAILY)
    op.execute(STAGES)
    op.execute(MATERIALIZED)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_canonical_eval_set_item ON mv_canonical_eval_set (eval_item_id)")
    op.execute(REFRESH)
    functions = ("tenant_daily_cost(date, date, uuid)", "tenant_stage_cost(date, date, uuid)", "refresh_canonical_eval_set()")
    for fn in functions:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
    if _has_role(_VIEW_OWNER):
        op.execute(f"GRANT SELECT ON pipeline_run, stage_execution, eval_set, eval_item TO {_VIEW_OWNER}")
        op.execute(f"ALTER MATERIALIZED VIEW mv_canonical_eval_set OWNER TO {_VIEW_OWNER}")
        for fn in functions:
            op.execute(f"ALTER FUNCTION {fn} OWNER TO {_VIEW_OWNER}")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT ON mv_canonical_eval_set TO {_APP_ROLE}")
        for fn in functions:
            op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS refresh_canonical_eval_set()")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_canonical_eval_set")
    op.execute("DROP FUNCTION IF EXISTS tenant_stage_cost(date, date, uuid)")
    op.execute("DROP FUNCTION IF EXISTS tenant_daily_cost(date, date, uuid)")
