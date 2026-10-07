"""How each table is doing: dead rows waiting for vacuum, when vacuum last ran, and how much space it takes.

Order: read the statistics views (table_stats), which any role may read and which hold no lab data
-> flag tables whose dead rows outgrow their live ones (BLOAT_RATIO) -> measure what compression
saves on a column (compression_ratio; needs a role that can read every row, so the owner).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

#: The columns migration 0016 compresses: transcripts, rendered reports and per-stage JSON.
COMPRESSED_COLUMNS: tuple[tuple[str, str], ...] = (("transcript", "text"), ("transcript_utterance", "text"), ("verbatim_transcript", "text"), ("corpus_report", "report_text"), ("report_draft", "rendered_text"), ("report_draft", "structured_payload"), ("report_revision", "rendered_text"), ("report_revision", "structured_payload"), ("final_report", "rendered_text"), ("final_report", "structured_payload"), ("stage_execution", "input_ref"), ("stage_execution", "output_ref"), ("asr_run", "raw_response"), ("template_version", "json_schema"), ("eval_item", "gold_transcript_verbatim"), ("audit_log", "before"), ("audit_log", "after"))

#: A table is bloated when dead rows exceed this share of all rows, and there are enough of them to matter.
BLOAT_RATIO = 0.2
BLOAT_MIN_DEAD = 1000


@dataclass(frozen=True, slots=True)
class TableStats:
    table: str
    live_rows: int
    dead_rows: int
    dead_ratio: float
    total_bytes: int
    last_autovacuum: str | None
    last_autoanalyze: str | None
    autovacuum_count: int
    bloated: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def table_stats(session: Session, *, limit: int = 30) -> list[TableStats]:
    """The biggest tables (partitions rolled up under their parent) with their vacuum state."""
    rows = session.execute(
        text(
            """
            SELECT COALESCE(parent.relname, s.relname) AS name,
                   sum(s.n_live_tup), sum(s.n_dead_tup), sum(pg_total_relation_size(s.relid)),
                   max(s.last_autovacuum), max(s.last_autoanalyze), sum(s.autovacuum_count)
            FROM pg_stat_user_tables s
            LEFT JOIN pg_inherits i ON i.inhrelid = s.relid
            LEFT JOIN pg_class parent ON parent.oid = i.inhparent
            WHERE s.schemaname = 'public' AND s.relname NOT LIKE 'archive\\_%'
            GROUP BY 1
            ORDER BY 4 DESC
            LIMIT :n
            """
        ),
        {"n": limit},
    ).all()
    out = []
    for name, live, dead, size, vacuumed, analysed, count in rows:
        live, dead = int(live or 0), int(dead or 0)
        ratio = dead / (live + dead) if live + dead else 0.0
        out.append(TableStats(table=name, live_rows=live, dead_rows=dead, dead_ratio=round(ratio, 4), total_bytes=int(size or 0), last_autovacuum=vacuumed.isoformat() if vacuumed else None, last_autoanalyze=analysed.isoformat() if analysed else None, autovacuum_count=int(count or 0), bloated=ratio > BLOAT_RATIO and dead >= BLOAT_MIN_DEAD))
    return out


def compression_ratio(session: Session, table: str, column: str) -> dict[str, Any]:
    """Raw bytes against stored bytes for one column, and the method each value is stored with."""
    if not table.isidentifier() or not column.isidentifier():
        raise ValueError("table and column must be plain identifiers")
    raw, stored, methods = session.execute(text(f'SELECT COALESCE(sum(octet_length("{column}"::text)), 0), COALESCE(sum(pg_column_size("{column}")), 0), array_agg(DISTINCT pg_column_compression("{column}")) FROM {table}')).one()
    return {"table": table, "column": column, "raw_bytes": int(raw), "stored_bytes": int(stored), "saved": round(1 - stored / raw, 4) if raw else 0.0, "methods": sorted(m for m in (methods or []) if m)}
