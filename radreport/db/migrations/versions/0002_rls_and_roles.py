"""Turns on row-level security and splits the database roles, so one lab's rows are unreachable from another lab's connection.

Order: upgrade creates the roles, enables the per-row policies and builds the few named
cross-lab views; downgrade removes them.
"""

from __future__ import annotations

from alembic import op

from radreport.db.introspect import classify_tables

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

#: The session variable every policy reads. Matches `core.tenancy.TENANT_GUC`.
_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"

APP_ROLE = "radreport_app"
AUDIT_ROLE = "radreport_audit"
VIEW_OWNER_ROLE = "radreport_views"


def _create_role(name: str, *, attributes: str = "") -> None:
    """Idempotent role creation — `CREATE ROLE` has no `IF NOT EXISTS`."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN
                CREATE ROLE {name} NOLOGIN {attributes};
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    classification = classify_tables()
    strict = sorted(classification.strict)
    nullable = sorted(classification.nullable)

    # ---------------------------------------------------------------- roles --
    # Privilege containers, not login users.
    _create_role(APP_ROLE)
    _create_role(AUDIT_ROLE)
    # The ONLY role with BYPASSRLS, and it cannot log in. It exists solely to
    # own the three enumerated cross-tenant views below.
    _create_role(VIEW_OWNER_ROLE, attributes="BYPASSRLS")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}, {AUDIT_ROLE}, {VIEW_OWNER_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}")

    # ------------------------------------------------------- audit_log grants --
    # append-only, on a separate database role with INSERT-only grants.
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT, SELECT ON audit_log TO {AUDIT_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE audit_log_id_seq TO {AUDIT_ROLE}")

    # ----------------------------------------------------------------- RLS ---
    for table in strict:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {_TENANT})
                WITH CHECK (tenant_id = {_TENANT})
            """
        )

    for table in nullable:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # NULL = global / canonical: visible to every tenant, writable only by
        # a session with no tenant bound (`system_session`).
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id IS NULL OR tenant_id = {_TENANT})
                WITH CHECK (
                    (tenant_id IS NULL AND {_TENANT} IS NULL)
                    OR tenant_id = {_TENANT}
                )
            """
        )

    # --------------------------------------------- enumerated cross-tenant ---
    # "Keep these in a separate, named set of views with their own access path.
    op.execute(
        """
        CREATE VIEW v_tenant_metering_rollup AS
        SELECT
            pr.tenant_id,
            date_trunc('month', pr.created_at)     AS period,
            count(*)                               AS run_count,
            sum(pr.total_cost_usd)                 AS total_cost_usd,
            avg(pr.total_cost_usd)                 AS avg_cost_usd
        FROM pipeline_run pr
        WHERE pr.is_shadow = false
        GROUP BY pr.tenant_id, date_trunc('month', pr.created_at)
        """
    )
    op.execute(
        """
        CREATE VIEW v_canonical_eval_set AS
        SELECT es.id, es.name, es.is_frozen, ei.id AS eval_item_id,
               ei.source_tenant_id, ei.capture_device_class, ei.audio_quality_bucket
        FROM eval_set es
        JOIN eval_item ei ON ei.eval_set_id = es.id
        WHERE es.tenant_id IS NULL
        """
    )
    for view in ("v_tenant_metering_rollup", "v_canonical_eval_set"):
        op.execute(f"ALTER VIEW {view} OWNER TO {VIEW_OWNER_ROLE}")
        op.execute(f"GRANT SELECT ON {view} TO {APP_ROLE}")

    # `tenant` itself is the third cross-tenant read (the lab list). It carries
    # no tenant column, so it needs no policy — only a grant.
    op.execute(f"GRANT SELECT ON tenant TO {APP_ROLE}")


def downgrade() -> None:
    classification = classify_tables()
    op.execute("DROP VIEW IF EXISTS v_canonical_eval_set")
    op.execute("DROP VIEW IF EXISTS v_tenant_metering_rollup")
    for table in sorted(classification.strict | classification.nullable):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
