"""The index report runs against a real database and stops flagging the keys migration 0011 indexed."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from radreport.devtools import query_report

pytestmark = pytest.mark.db


def test_the_new_lookup_indexes_are_not_reported_missing(migrated_db: str) -> None:
    flagged = {(fk.table, fk.columns) for fk in query_report.unindexed_foreign_keys(create_engine(migrated_db))}
    assert ("pipeline_run", ("recording_id", "tenant_id")) not in flagged
    assert ("report_draft", ("recording_id", "tenant_id")) not in flagged
    assert ("corpus_report", ("import_batch_id", "tenant_id")) not in flagged


def test_a_select_gets_a_generic_plan_without_running(migrated_db: str) -> None:
    plan = query_report.explain_generic(create_engine(migrated_db), "SELECT id FROM pipeline_run WHERE tenant_id = $1 AND recording_id = $2")
    assert plan is not None and "pipeline_run" in plan
    assert query_report.explain_generic(create_engine(migrated_db), "DELETE FROM tenant WHERE id = $1") is None
