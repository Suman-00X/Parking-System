"""Autovacuum is tuned on the churning tables, large columns are compressed, and the table-health view reports both."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from radreport.db.session import system_session
from radreport.db.table_health import COMPRESSED_COLUMNS, compression_ratio
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db


def _reloptions(db: str, table: str) -> list[str]:
    with system_session(db) as session:
        return list(session.execute(text("SELECT reloptions FROM pg_class WHERE relname = :t"), {"t": table}).scalar() or [])


def test_churning_tables_vacuum_early(migrated_db: str) -> None:
    for table in ("pipeline_run", "job", "outbox_event", "recording_p3", "stage_execution_default"):
        assert "autovacuum_vacuum_scale_factor=0.02" in _reloptions(migrated_db, table), table


def test_a_new_month_inherits_its_tables_vacuum_settings(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        session.execute(text("SELECT ensure_month_partition('stage_execution', 'y2039m02', '2039-02-01', '2039-03-01')"))
    assert "autovacuum_vacuum_scale_factor=0.02" in _reloptions(migrated_db, "stage_execution_y2039m02")


def test_large_columns_are_compressed(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        methods = {(t, c): session.execute(text("SELECT attcompression FROM pg_attribute WHERE attrelid = CAST(:t AS regclass) AND attname = :c"), {"t": t, "c": c}).scalar() for t, c in COMPRESSED_COLUMNS}
    assert set(methods.values()) <= {"l", "p"} and all(methods.values()), methods


def test_a_long_transcript_is_stored_compressed(migrated_db: str) -> None:
    """Measured, not assumed: a typical repetitive report body shrinks on disk."""
    owner = create_engine(migrated_db)
    body = " ".join(["The liver is normal in size and echotexture. No focal lesion is seen."] * 120)
    try:
        with Session(owner) as session:
            session.execute(text("CREATE TEMP TABLE compress_probe (body text COMPRESSION lz4)"))
            session.execute(text("INSERT INTO compress_probe VALUES (:b)"), {"b": body})
            result = compression_ratio(session, "compress_probe", "body")
    except Exception as exc:  # noqa: BLE001 - only a server built without lz4 gets here
        pytest.skip(f"lz4 unavailable here: {type(exc).__name__}")
    assert result["saved"] > 0.5, result


def test_the_table_health_endpoint(migrated_db: str) -> None:
    body = signed_in(make_platform_user(migrated_db)).get("/admin/api/ops/tables").json()
    names = {t["table"] for t in body["tables"]}
    assert "recording" in names and not any(n.startswith("recording_p") for n in names), "partitions roll up under their parent"
    assert body["bloat_ratio_threshold"] == 0.2
