"""Partitions recording by lab and stage_execution by month, keeps monthly partitions coming, and closes direct access to partitions.

Order: upgrade replaces ensure_month_partition with a version that can split rows out of the default
partition and runs as the table owner, adds detach_month_partitions to archive old months, rebuilds
recording as eight hash partitions on tenant_id and stage_execution as monthly range partitions
(copying rows and restoring every key, index, foreign key and policy from the models), makes
eval_item reach recording through its source lab, and revokes the app role's direct access to every
partition, which carries no row-level policy of its own; downgrade restores the plain tables.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from alembic import op
from sqlalchemy.schema import AddConstraint, CreateIndex

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_APP_ROLE = "radreport_app"
RECORDING_PARTITIONS = 8
MONTHLY = ("asr_segment", "edit_event", "audit_log", "stage_execution")

ENSURE = """
CREATE OR REPLACE FUNCTION ensure_month_partition(parent text, suffix text, range_start date, range_end date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    child text := format('%s_%s', parent, suffix);
    dflt text := parent || '_default';
    keycol text;
    waiting bigint := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = child) THEN
        RETURN;
    END IF;
    SELECT a.attname INTO keycol FROM pg_partitioned_table p JOIN pg_attribute a ON a.attrelid = p.partrelid AND a.attnum = p.partattrs[0] WHERE p.partrelid = parent::regclass;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = dflt) THEN
        EXECUTE format('SELECT count(*) FROM %I WHERE %I >= %L AND %I < %L', dflt, keycol, range_start, keycol, range_end) INTO waiting;
    END IF;
    IF waiting > 0 THEN
        -- Rows that landed in the default partition would make CREATE ... PARTITION OF fail; move them into the new month.
        EXECUTE format('ALTER TABLE %I DETACH PARTITION %I', parent, dflt);
        EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)', child, parent, range_start, range_end);
        EXECUTE format('INSERT INTO %I SELECT * FROM %I WHERE %I >= %L AND %I < %L', child, dflt, keycol, range_start, keycol, range_end);
        EXECUTE format('DELETE FROM %I WHERE %I >= %L AND %I < %L', dflt, keycol, range_start, keycol, range_end);
        EXECUTE format('ALTER TABLE %I ATTACH PARTITION %I DEFAULT', parent, dflt);
    ELSE
        EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)', child, parent, range_start, range_end);
    END IF;
    -- A partition has no row-level policy of its own: only the parent, where the policy is, may be read.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radreport_app') THEN
        EXECUTE format('REVOKE ALL ON %I FROM radreport_app', child);
    END IF;
END $$;
"""

DETACH = """
CREATE OR REPLACE FUNCTION detach_month_partitions(parent text, keep_from date)
RETURNS SETOF text LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    part record;
    archived text;
BEGIN
    FOR part IN
        SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = parent::regclass AND c.relname ~ ('^' || parent || '_y[0-9]{4}m[0-9]{2}$')
           AND to_date(substring(c.relname from '_y([0-9]{4})m') || substring(c.relname from 'm([0-9]{2})$') || '01', 'YYYYMMDD') < date_trunc('month', keep_from)
         ORDER BY 1
    LOOP
        -- Detached, not dropped: the month becomes a plain archive table for ops to dump or drop.
        EXECUTE format('ALTER TABLE %I DETACH PARTITION %I', parent, part.relname);
        archived := 'archive_' || part.relname;
        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = archived) THEN
            archived := archived || '_' || extract(epoch FROM clock_timestamp())::bigint;
        END IF;
        EXECUTE format('ALTER TABLE %I RENAME TO %I', part.relname, archived);
        RETURN NEXT archived;
    END LOOP;
END $$;
"""


def _bind() -> sa.engine.Connection:
    return op.get_bind()


def _relkind(table: str) -> str | None:
    return _bind().execute(sa.text("SELECT relkind FROM pg_class WHERE relname = :t AND relnamespace = 'public'::regnamespace"), {"t": table}).scalar()


def _has_role(role: str) -> bool:
    return bool(_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first())


def _month(anchor: dt.date, offset: int) -> tuple[str, dt.date, dt.date]:
    month = anchor.month - 1 + offset
    year = anchor.year + month // 12
    month = month % 12 + 1
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), month % 12 + 1, 1)
    return f"y{start:%Y}m{start:%m}", start, end


def _inbound_foreign_keys(table: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in _bind().execute(sa.text("SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype = 'f' AND confrelid = CAST(:t AS regclass)"), {"t": table}).all()]


def _rebuild(table: str, partition_by: str) -> None:
    """Replace a plain table with a partitioned one of the same columns, then restore its keys and indexes from the models."""
    from radreport.db.models import Base

    model = Base.metadata.tables[table]
    for child, name in _inbound_foreign_keys(table):
        op.execute(f'ALTER TABLE {child} DROP CONSTRAINT "{name}"')
    op.execute(f"ALTER TABLE {table} RENAME TO {table}_unpartitioned")
    op.execute(f"CREATE TABLE {table} (LIKE {table}_unpartitioned INCLUDING DEFAULTS) PARTITION BY {partition_by}")
    _create_partitions(table)
    op.execute(f"INSERT INTO {table} SELECT * FROM {table}_unpartitioned")
    op.execute(f"DROP TABLE {table}_unpartitioned")
    for constraint in model.constraints:
        op.execute(AddConstraint(constraint))
    for index in model.indexes:
        op.execute(CreateIndex(index))
    for other in Base.metadata.sorted_tables:
        for fk in other.foreign_key_constraints:
            if fk.referred_table is model and other is not model:
                op.execute(AddConstraint(fk))
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})")
    if _has_role(_APP_ROLE):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_APP_ROLE}")


def _create_partitions(table: str) -> None:
    if table == "recording":
        for remainder in range(RECORDING_PARTITIONS):
            op.execute(f"CREATE TABLE IF NOT EXISTS recording_p{remainder} PARTITION OF recording FOR VALUES WITH (MODULUS {RECORDING_PARTITIONS}, REMAINDER {remainder})")
    else:
        op.execute(f"CREATE TABLE IF NOT EXISTS {table}_default PARTITION OF {table} DEFAULT")
        today = dt.date.today().replace(day=1)
        for offset in range(-1, 4):
            suffix, start, end = _month(today, offset)
            op.execute(f"SELECT ensure_month_partition('{table}', '{suffix}', '{start.isoformat()}', '{end.isoformat()}')")


def _revoke_partitions() -> None:
    if not _has_role(_APP_ROLE):
        return
    for (name,) in _bind().execute(sa.text("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent WHERE p.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')")).all():
        op.execute(f'REVOKE ALL ON "{name}" FROM {_APP_ROLE}')


def upgrade() -> None:
    """Guarded on what the database already is: a fresh database got partitioned tables from 0001 and only needs their partitions."""
    op.execute(ENSURE)
    op.execute(DETACH)
    for fn in ("ensure_month_partition(text, text, date, date)", "detach_month_partitions(text, date)"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        if _has_role(_APP_ROLE):
            op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {_APP_ROLE}")

    # eval_item must name its recording's lab before recording can be keyed by (id, tenant_id).
    op.execute("UPDATE eval_item ei SET source_tenant_id = r.tenant_id FROM recording r WHERE ei.recording_id = r.id AND ei.source_tenant_id IS NULL")
    op.execute("ALTER TABLE eval_item ALTER COLUMN source_tenant_id SET NOT NULL")

    if _relkind("recording") == "r":
        _rebuild("recording", "HASH (tenant_id)")
    else:
        _create_partitions("recording")
    if _relkind("stage_execution") == "r":
        _rebuild("stage_execution", "RANGE (created_at)")
    else:
        _create_partitions("stage_execution")

    today = dt.date.today().replace(day=1)
    for parent in MONTHLY:
        for offset in range(0, 4):
            suffix, start, end = _month(today, offset)
            op.execute(f"SELECT ensure_month_partition('{parent}', '{suffix}', '{start.isoformat()}', '{end.isoformat()}')")
    _revoke_partitions()


def downgrade() -> None:
    """Back to plain tables. Rows are copied; the new functions are left in place, they are harmless."""
    from radreport.db.models import Base

    for table in ("stage_execution", "recording"):
        if _relkind(table) != "p":
            continue
        for child, name in _inbound_foreign_keys(table):
            op.execute(f'ALTER TABLE {child} DROP CONSTRAINT "{name}"')
        op.execute(f"ALTER TABLE {table} RENAME TO {table}_partitioned")
        op.execute(f"CREATE TABLE {table} (LIKE {table}_partitioned INCLUDING DEFAULTS)")
        op.execute(f"INSERT INTO {table} SELECT * FROM {table}_partitioned")
        op.execute(f"DROP TABLE {table}_partitioned CASCADE")
        model = Base.metadata.tables[table]
        for constraint in model.constraints:
            op.execute(AddConstraint(constraint))
        for index in model.indexes:
            op.execute(CreateIndex(index))
        for other in Base.metadata.sorted_tables:
            for fk in other.foreign_key_constraints:
                if fk.referred_table is model and other is not model:
                    op.execute(AddConstraint(fk))
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})")
        if _has_role(_APP_ROLE):
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_APP_ROLE}")
