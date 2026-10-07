"""Partitions: kept ahead of the calendar, rows rescued from the default partition, old months archived, and closed to direct reads."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from radreport.db.session import system_session, tenant_session
from radreport.workers import schedule
from radreport.workers.maintenance import ensure_partitions_now, month_bounds

pytestmark = pytest.mark.db


def _exists(db: str, name: str) -> bool:
    with system_session(db) as session:
        return bool(session.execute(text("SELECT 1 FROM pg_class WHERE relname = :n"), {"n": name}).first())


def test_partitions_are_created_months_ahead(migrated_db: str) -> None:
    far = dt.date(2031, 1, 1)
    with system_session(migrated_db) as session:
        result = ensure_partitions_now(session, today=far)
    for offset in range(4):
        suffix, _s, _e = month_bounds(far, offset)
        for table in ("asr_segment", "edit_event", "audit_log", "stage_execution"):
            assert _exists(migrated_db, f"{table}_{suffix}")
    assert result["months_ahead"] == 3


def test_a_row_in_the_default_partition_is_moved_when_its_month_arrives(migrated_db: str, two_tenants) -> None:
    """The bug this fixes: once the default partition held a month's rows, creating that month's partition failed."""
    from radreport.core.types import PipelineTrigger
    from radreport.pipeline.graph import new_run

    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
        from radreport.db.models.ingestion import Recording

        user = AppUser(tenant_id=tenant_id, employee_code=f"P-{uuid.uuid4().hex[:6]}", display_name="Dr P", roles=["radiologist"])
        session.add(user)
        session.flush()
        profile = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
        patient = Patient(tenant_id=tenant_id, mrn=f"M{uuid.uuid4().hex[:8]}", pseudonym=f"P{uuid.uuid4().hex[:8]}")
        session.add_all([profile, patient])
        session.flush()
        study = Study(tenant_id=tenant_id, patient_id=patient.id)
        session.add(study)
        session.flush()
        recording = Recording(tenant_id=tenant_id, study_id=study.id, radiologist_id=profile.id, object_key="k", content_hash=uuid.uuid4().hex, capture_device_class="dictation_mic_ptt", audio_format="flac")
        session.add(recording)
        session.flush()
        run, _ctx, _state = new_run(session, tenant_id=tenant_id, recording_id=recording.id, trigger=PipelineTrigger.UPLOAD)
        session.execute(text("INSERT INTO stage_execution (tenant_id, pipeline_run_id, stage_name, input_ref, status, created_at) VALUES (:t, :r, 'future', '{}', 'succeeded', '2040-05-15')"), {"t": tenant_id, "r": run.id})
    with system_session(migrated_db) as session:
        session.execute(text("SELECT ensure_month_partition('stage_execution', 'y2040m05', '2040-05-01', '2040-06-01')"))
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert session.execute(text("SELECT tableoid::regclass::text FROM stage_execution WHERE stage_name = 'future'")).scalar_one() == "stage_execution_y2040m05"


def test_months_past_retention_are_archived(migrated_db: str) -> None:
    from radreport.core import system_config

    with system_session(migrated_db) as session:
        session.execute(text("SELECT ensure_month_partition('stage_execution', 'y2001m01', '2001-01-01', '2001-02-01')"))
        system_config.set_value(session, "retention.stage_execution_months", 12, actor_id=uuid.uuid4())
    try:
        with system_session(migrated_db) as session:
            result = ensure_partitions_now(session)
        assert any(name.startswith("archive_stage_execution_y2001m01") for name in result["archived"])
        assert not _exists(migrated_db, "stage_execution_y2001m01")
        assert _exists(migrated_db, f"stage_execution_{month_bounds(dt.date.today(), 0)[0]}"), "the current month is never archived"
    finally:
        with system_session(migrated_db) as session:
            system_config.reset_value(session, "retention.stage_execution_months", actor_id=uuid.uuid4())


def test_the_audit_trail_is_never_archived(migrated_db: str) -> None:
    from radreport.workers.maintenance import MONTHLY_TABLES

    assert MONTHLY_TABLES["audit_log"] is None and MONTHLY_TABLES["edit_event"] is None


def test_periodic_jobs_are_queued_once_per_period(migrated_db: str) -> None:
    moment = 9_000_000_000.0  # a period no other test uses
    with system_session(migrated_db) as session:
        first = schedule.enqueue_due(session, now=moment)
        second = schedule.enqueue_due(session, now=moment + 1)
        session.execute(text("DELETE FROM job WHERE dedupe_key LIKE '%:' || :b"), {"b": str(int(moment // 600))})
        session.execute(text("DELETE FROM job WHERE dedupe_key LIKE '%:' || :b"), {"b": str(int(moment // (6 * 3600)))})
    assert set(first) == {"reap_jobs", "ensure_partitions"} and second == []


def test_partitions_cannot_be_read_around_the_row_level_policy(app_db_url: str, migrated_db: str) -> None:
    """Partitions carry no policy of their own; only the parent, where the policy is, may be read."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import ProgrammingError

    engine = create_engine(app_db_url)
    for partition in ("recording_p0", "edit_event_default", "audit_log_default", "stage_execution_default"):
        with engine.connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(f"SELECT 1 FROM {partition} LIMIT 1"))


def test_queries_touch_only_their_partition(migrated_db: str, two_tenants) -> None:
    """What partitioning buys: a month's cost query scans one month, a lab's recordings scan one hash partition."""
    tenant_id, _ = two_tenants
    month = month_bounds(dt.date.today(), 0)
    with system_session(migrated_db) as session:
        stages = "\n".join(session.execute(text("EXPLAIN SELECT sum(cost_usd) FROM stage_execution WHERE created_at >= :a AND created_at < :b"), {"a": month[1], "b": month[2]}).scalars())
        recordings = "\n".join(session.execute(text(f"EXPLAIN SELECT count(*) FROM recording WHERE tenant_id = '{tenant_id}'")).scalars())
    assert f"stage_execution_{month[0]}" in stages and "stage_execution_default" not in stages
    assert sum(f"recording_p{i}" in recordings for i in range(8)) == 1
