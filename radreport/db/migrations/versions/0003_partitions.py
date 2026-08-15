"""Splits the three fastest-growing tables into monthly partitions.

Order: upgrade partitions the speech segments, reviewer edits and audit log; downgrade merges
them back.
"""

from __future__ import annotations

import datetime as dt

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

PARTITIONED: tuple[tuple[str, str], ...] = (("asr_segment", "created_at"), ("edit_event", "created_at"), ("audit_log", "occurred_at"))


def _month_bounds(anchor: dt.date, offset: int) -> tuple[str, dt.date, dt.date]:
    month = anchor.month - 1 + offset
    year = anchor.year + month // 12
    month = month % 12 + 1
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), month % 12 + 1, 1)
    return f"y{start:%Y}m{start:%m}", start, end


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_month_partition(
            parent text, suffix text, range_start date, range_end date
        ) RETURNS void AS $$
        DECLARE
            child text := format('%s_%s', parent, suffix);
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = child) THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                    child, parent, range_start, range_end
                );
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    today = dt.date.today().replace(day=1)
    for parent, _column in PARTITIONED:
        # A default partition means a late or clock-skewed row lands somewhere instead of failing the insert.
        op.execute(f"CREATE TABLE IF NOT EXISTS {parent}_default PARTITION OF {parent} DEFAULT")
        # One month back (late-arriving rows) through twelve ahead.
        for offset in range(-1, 13):
            suffix, start, end = _month_bounds(today, offset)
            op.execute(f"SELECT ensure_month_partition('{parent}', '{suffix}', '{start.isoformat()}', '{end.isoformat()}')")


def downgrade() -> None:
    today = dt.date.today().replace(day=1)
    for parent, _column in PARTITIONED:
        for offset in range(-1, 13):
            suffix, _start, _end = _month_bounds(today, offset)
            op.execute(f"DROP TABLE IF EXISTS {parent}_{suffix}")
        op.execute(f"DROP TABLE IF EXISTS {parent}_default")
    op.execute("DROP FUNCTION IF EXISTS ensure_month_partition(text, text, date, date)")
