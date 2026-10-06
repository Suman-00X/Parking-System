"""Finds the statements that cost the most and the indexes that are missing, from the database's own statistics.

Order: read the heaviest statements (top_statements) and plan each one without running it
(explain_generic) -> find foreign keys with no index behind them (unindexed_foreign_keys) ->
find tables read mostly by sequential scan (seq_scan_heavy_tables) and indexes nothing uses
(unused_indexes) -> print it all as markdown (render_report, main).

Run as the database owner or a member of pg_read_all_stats: pg_stat_statements hides other
roles' statement text from everyone else.

    python -m radreport.devtools.query_report --url postgresql+psycopg://owner@localhost/radreport --top 20
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, text


@dataclass(frozen=True, slots=True)
class Statement:
    query: str
    calls: int
    total_ms: float
    mean_ms: float
    rows: int
    shared_blks_read: int


@dataclass(frozen=True, slots=True)
class UnindexedForeignKey:
    table: str
    constraint: str
    columns: tuple[str, ...]
    table_rows: int


@dataclass(frozen=True, slots=True)
class TableScans:
    table: str
    seq_scan: int
    seq_tup_read: int
    idx_scan: int
    live_rows: int


def top_statements(engine: Engine, *, limit: int = 20) -> list[Statement]:
    """The statements with the most total execution time, from pg_stat_statements."""
    with engine.connect() as conn:
        present = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")).first()
        if not present:
            raise RuntimeError("pg_stat_statements is not installed here; run `make pg-observe` and restart Postgres")
        rows = conn.execute(text("SELECT query, calls, total_exec_time, mean_exec_time, rows, shared_blks_read FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) AND query !~* '^\\s*(BEGIN|COMMIT|ROLLBACK|SET|SHOW|SAVEPOINT|RELEASE)' AND query !~* 'pg_stat_statements|pg_catalog|information_schema' ORDER BY total_exec_time DESC LIMIT :n"), {"n": limit}).all()
    return [Statement(query=" ".join(r[0].split()), calls=int(r[1]), total_ms=float(r[2]), mean_ms=float(r[3]), rows=int(r[4]), shared_blks_read=int(r[5])) for r in rows]


def explain_generic(engine: Engine, query: str) -> str | None:
    """The generic plan of a parameterised SELECT, without executing it; None for anything else."""
    if not query.lstrip().lower().startswith(("select", "with")):
        return None
    with engine.connect() as conn:
        try:
            plan = conn.execute(text(f"EXPLAIN (GENERIC_PLAN, COSTS true) {query}")).scalars().all()
        except Exception as exc:  # noqa: BLE001 - a plan we cannot build is reported, not fatal
            return f"(no plan: {type(exc).__name__})"
        finally:
            conn.rollback()
    return "\n".join(plan)


def unindexed_foreign_keys(engine: Engine) -> list[UnindexedForeignKey]:
    """Foreign keys whose referencing columns are not the leading columns of any index, so a parent delete scans the child."""
    sql = """
        SELECT c.conrelid::regclass::text AS tbl, c.conname,
               array_agg(a.attname ORDER BY k.ord) AS cols,
               COALESCE(s.n_live_tup, 0) AS live
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.conrelid
        WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
          AND NOT EXISTS (
              SELECT 1 FROM pg_index i
              WHERE i.indrelid = c.conrelid
                AND (i.indkey::int2[])[0:cardinality(c.conkey) - 1] @> c.conkey
                AND (i.indkey::int2[])[0:cardinality(c.conkey) - 1] <@ c.conkey
          )
          AND NOT EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = c.conrelid)
        GROUP BY c.conrelid, c.conname, s.n_live_tup
        ORDER BY 1, 2
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).all()
    return [UnindexedForeignKey(table=r[0], constraint=r[1], columns=tuple(r[2]), table_rows=int(r[3])) for r in rows]


def seq_scan_heavy_tables(engine: Engine, *, limit: int = 15) -> list[TableScans]:
    """Tables where sequential scans read the most rows."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT relname, seq_scan, seq_tup_read, COALESCE(idx_scan, 0), n_live_tup FROM pg_stat_user_tables WHERE seq_scan > 0 ORDER BY seq_tup_read DESC LIMIT :n"), {"n": limit}).all()
    return [TableScans(table=r[0], seq_scan=int(r[1]), seq_tup_read=int(r[2]), idx_scan=int(r[3]), live_rows=int(r[4])) for r in rows]


def unused_indexes(engine: Engine) -> list[tuple[str, str, int]]:
    """Non-unique indexes that have never been scanned: write cost with no read benefit."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT s.relname, s.indexrelname, pg_relation_size(s.indexrelid) FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary ORDER BY pg_relation_size(s.indexrelid) DESC, 1, 2")).all()
    return [(r[0], r[1], int(r[2])) for r in rows]


def render_report(engine: Engine, *, top: int = 20) -> str:
    """Everything above as one markdown document."""
    lines = ["## Heaviest statements (pg_stat_statements, by total time)", ""]
    for n, st in enumerate(top_statements(engine, limit=top), start=1):
        lines += [f"### {n}. {st.calls} calls, {st.total_ms:.1f} ms total, {st.mean_ms:.3f} ms mean, {st.rows} rows", "", "```sql", st.query[:1200], "```"]
        plan = explain_generic(engine, st.query)
        if plan:
            lines += ["", "```", plan, "```"]
        lines.append("")
    lines += ["## Foreign keys with no supporting index", "", "| Table | Constraint | Columns | Rows |", "|---|---|---|---:|"]
    lines += [f"| {fk.table} | {fk.constraint} | {', '.join(fk.columns)} | {fk.table_rows} |" for fk in unindexed_foreign_keys(engine)]
    lines += ["", "## Tables read mostly by sequential scan", "", "| Table | Seq scans | Rows read by seq scan | Index scans | Live rows |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {t.table} | {t.seq_scan} | {t.seq_tup_read} | {t.idx_scan} | {t.live_rows} |" for t in seq_scan_heavy_tables(engine)]
    lines += ["", "## Indexes never scanned", "", "| Table | Index | Bytes |", "|---|---|---:|"]
    lines += [f"| {t} | {i} | {b} |" for t, i, b in unused_indexes(engine)]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="an owner or pg_read_all_stats connection URL")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--reset", action="store_true", help="clear pg_stat_statements and table counters, then exit")
    args = parser.parse_args(argv)
    engine = create_engine(args.url)
    if args.reset:
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_stat_statements_reset()"))
            conn.execute(text("SELECT pg_stat_reset()"))
        print("statistics reset")
        return
    print(render_report(engine, top=args.top))


if __name__ == "__main__":
    main()
