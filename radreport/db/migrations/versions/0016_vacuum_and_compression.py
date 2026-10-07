"""Tunes autovacuum on the high-churn tables and compresses the large text and JSON columns.

Order: upgrade picks lz4 when this server was built with it, else pglz, and sets it on each large
column (it applies to values written from now on; existing rows keep their compression until
rewritten) -> sets aggressive autovacuum thresholds on the tables rewritten most often and on every
leaf partition of the partitioned ones -> redefines ensure_month_partition so a new month copies
those settings from its default partition; downgrade resets them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

#: (table, column): the columns that hold transcripts, rendered reports and per-stage JSON.
COMPRESSED: tuple[tuple[str, str], ...] = (("transcript", "text"), ("transcript_utterance", "text"), ("verbatim_transcript", "text"), ("corpus_report", "report_text"), ("report_draft", "rendered_text"), ("report_draft", "structured_payload"), ("report_revision", "rendered_text"), ("report_revision", "structured_payload"), ("final_report", "rendered_text"), ("final_report", "structured_payload"), ("stage_execution", "input_ref"), ("stage_execution", "output_ref"), ("asr_run", "raw_response"), ("template_version", "json_schema"), ("eval_item", "gold_transcript_verbatim"), ("audit_log", "before"), ("audit_log", "after"))

#: Vacuum after 2% of rows change instead of the default 20%, analyse after 1%, and let the worker do more per pass.
AGGRESSIVE = "autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01, autovacuum_vacuum_cost_limit = 2000"
RESET = "autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, autovacuum_vacuum_cost_limit"
PLAIN = ("pipeline_run", "report_draft", "report_field_value", "job", "outbox_event", "rate_limit_counter")
PARTITIONED = ("recording", "stage_execution", "asr_segment")

ENSURE = """
CREATE OR REPLACE FUNCTION ensure_month_partition(parent text, suffix text, range_start date, range_end date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    child text := format('%s_%s', parent, suffix);
    dflt text := parent || '_default';
    keycol text;
    waiting bigint := 0;
    options text[];
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = child) THEN
        RETURN;
    END IF;
    SELECT a.attname INTO keycol FROM pg_partitioned_table p JOIN pg_attribute a ON a.attrelid = p.partrelid AND a.attnum = p.partattrs[0] WHERE p.partrelid = parent::regclass;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = dflt) THEN
        EXECUTE format('SELECT count(*) FROM %I WHERE %I >= %L AND %I < %L', dflt, keycol, range_start, keycol, range_end) INTO waiting;
        SELECT reloptions INTO options FROM pg_class WHERE relname = dflt;
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
    -- The new month vacuums like the rest of its table.
    IF options IS NOT NULL THEN
        EXECUTE format('ALTER TABLE %I SET (%s)', child, array_to_string(options, ', '));
    END IF;
    -- A partition has no row-level policy of its own: only the parent, where the policy is, may be read.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radreport_app') THEN
        EXECUTE format('REVOKE ALL ON %I FROM radreport_app', child);
    END IF;
END $$;
"""


def _supports_lz4() -> bool:
    conn = op.get_bind()
    try:
        with conn.begin_nested():
            conn.execute(sa.text("SET LOCAL default_toast_compression = lz4"))
        return True
    except sa.exc.DBAPIError:
        return False


def _leaves(parent: str) -> list[str]:
    return list(op.get_bind().execute(sa.text("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = CAST(:p AS regclass) AND c.relkind = 'r'"), {"p": parent}).scalars())


def _exists(table: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_class WHERE relname = :t AND relnamespace = 'public'::regnamespace"), {"t": table}).first())


def upgrade() -> None:
    method = "lz4" if _supports_lz4() else "pglz"
    for table, column in COMPRESSED:
        if _exists(table):
            op.execute(f'ALTER TABLE {table} ALTER COLUMN "{column}" SET COMPRESSION {method}')
    for table in PLAIN:
        if _exists(table):
            op.execute(f"ALTER TABLE {table} SET ({AGGRESSIVE})")
    for parent in PARTITIONED:
        if _exists(parent):
            for leaf in _leaves(parent):
                op.execute(f"ALTER TABLE {leaf} SET ({AGGRESSIVE})")
    op.execute(ENSURE)


def downgrade() -> None:
    for table, column in COMPRESSED:
        if _exists(table):
            op.execute(f'ALTER TABLE {table} ALTER COLUMN "{column}" SET COMPRESSION DEFAULT')
    for table in PLAIN:
        if _exists(table):
            op.execute(f"ALTER TABLE {table} RESET ({RESET})")
    for parent in PARTITIONED:
        if _exists(parent):
            for leaf in _leaves(parent):
                op.execute(f"ALTER TABLE {leaf} RESET ({RESET})")
