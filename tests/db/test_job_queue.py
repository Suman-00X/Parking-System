"""The Postgres job queue: exclusive claims, reclaimed leases, poison jobs, retries, dedupe, lab isolation, and ingest -> worker -> pipeline."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest
from sqlalchemy import select, text

from radreport.core.types import JobStatus, RunStatus
from radreport.db.models.jobs import Job
from radreport.db.models.orchestration import PipelineRun
from radreport.db.session import system_session, tenant_session
from radreport.workers import handlers, queue
from radreport.workers.worker import Worker

pytestmark = pytest.mark.db

KIND = f"test_{uuid.uuid4().hex[:6]}"
calls: list[str] = []


@handlers.handler(KIND)
async def _noop(session, job):  # type: ignore[no-untyped-def]
    if job.payload.get("explode"):
        raise RuntimeError("boom")
    calls.append(str(job.id))
    return {"ok": True}


@pytest.fixture(autouse=True)
def _clear_test_jobs(migrated_db: str):
    yield
    with system_session(migrated_db) as session:
        session.execute(text("DELETE FROM job WHERE kind LIKE 'test_%' AND tenant_id IS NULL"))


def _status(db: str, job_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> Job:
    ctx = tenant_session(tenant_id, url=db) if tenant_id else system_session(db)
    with ctx as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_two_workers_never_claim_the_same_job(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        for n in range(60):
            queue.enqueue(session, KIND, {"n": n})
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()

    def drain(worker: str) -> None:
        while True:
            with system_session(migrated_db) as session:
                batch = queue.claim(session, worker_id=worker, kinds=[KIND], limit=4)
            if not batch:
                return
            with lock:
                claimed.extend(j.id for j in batch)

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 60
    assert len(set(claimed)) == 60, "a job was handed to two workers"


def test_a_killed_workers_job_is_reclaimed_after_its_lease(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        job_id = queue.enqueue(session, KIND)
    with system_session(migrated_db) as session:
        [first] = queue.claim(session, worker_id="doomed", kinds=[KIND], visibility_seconds=1)
    # "doomed" dies here: it never completes or renews.
    with system_session(migrated_db) as session:
        assert queue.claim(session, worker_id="other", kinds=[KIND]) == [], "still leased"
    time.sleep(1.2)
    with system_session(migrated_db) as session:
        [again] = queue.claim(session, worker_id="other", kinds=[KIND])
        assert again.id == first.id == job_id and again.attempts == 2
        assert queue.complete(session, again, worker_id="other")
    with system_session(migrated_db) as session:
        # The dead worker's late completion is refused: it no longer holds the lease.
        assert not queue.complete(session, first, worker_id="doomed")
    assert _status(migrated_db, job_id).status == JobStatus.SUCCEEDED


def test_a_poison_job_is_buried_after_its_attempts(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        job_id = queue.enqueue(session, KIND, max_attempts=2)
    for _ in range(2):
        with system_session(migrated_db) as session:
            assert queue.claim(session, worker_id="crashy", kinds=[KIND], visibility_seconds=1)
        time.sleep(1.1)
    with system_session(migrated_db) as session:
        assert queue.claim(session, worker_id="crashy", kinds=[KIND]) == []
    job = _status(migrated_db, job_id)
    assert job.status == JobStatus.DEAD and "final attempt" in (job.last_error or "")


def test_a_failing_handler_backs_off_then_dies(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        job_id = queue.enqueue(session, KIND, {"explode": True}, max_attempts=2)
    worker = Worker(kinds=[KIND], url=migrated_db)
    assert asyncio.run(worker.run_once()) == 1
    job = _status(migrated_db, job_id)
    assert job.status == JobStatus.QUEUED and "boom" in (job.last_error or "")
    with system_session(migrated_db) as session:
        session.execute(text("UPDATE job SET run_at = now() WHERE id = :id"), {"id": job_id})
    asyncio.run(worker.run_once())
    assert _status(migrated_db, job_id).status == JobStatus.DEAD


def test_a_successful_job_completes_in_its_handlers_transaction(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        job_id = queue.enqueue(session, KIND)
    assert asyncio.run(Worker(kinds=[KIND], url=migrated_db).run_once()) == 1
    job = _status(migrated_db, job_id)
    assert job.status == JobStatus.SUCCEEDED and job.result == {"ok": True} and str(job_id) in calls


def test_a_dedupe_key_queues_one_live_job(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        first = queue.enqueue(session, KIND, dedupe_key="same")
        second = queue.enqueue(session, KIND, dedupe_key="same")
    assert first is not None and second is None


def test_a_labs_jobs_are_invisible_to_another_lab(migrated_db: str, two_tenants) -> None:
    lab_a, lab_b = two_tenants
    with tenant_session(lab_a, url=migrated_db) as session:
        job_id = queue.enqueue(session, KIND, tenant_id=lab_a)
    with tenant_session(lab_b, url=migrated_db) as session:
        assert session.get(Job, job_id) is None
    with tenant_session(lab_a, url=migrated_db) as session:
        assert session.get(Job, job_id) is not None
        session.execute(text("DELETE FROM job WHERE id = :id"), {"id": job_id})


def test_an_upload_queues_a_pipeline_run_that_a_worker_executes(migrated_db: str, two_tenants, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from radreport.adapters.asr.whisper_local import StubASREngine
    from radreport.adapters.storage.object_store import InMemoryObjectStore
    from radreport.api.app import create_app
    from radreport.api.routes import ingest as ingest_route
    from radreport.devtools.synthetic import synth_audio
    from radreport.pipeline import runner
    from radreport.pipeline.stages.providers import StaticKnowledgeProvider, load_tenant_knowledge
    from radreport.pipeline.v1 import build_v1_graph
    from tests.integration.test_api_ingest import _post

    store = InMemoryObjectStore()
    monkeypatch.setattr(ingest_route, "S3ObjectStore", lambda _settings: store)
    runner.set_graph_factory(lambda session, tenant_id: (build_v1_graph(store=store, asr_engine=StubASREngine(text="study type ultrasound abdomen. no free fluid."), knowledge=StaticKnowledgeProvider(load_tenant_knowledge(session, tenant_id)), templates=runner.load_template_candidates(session, tenant_id), sections=[]), store))
    try:
        tenant_id, _ = two_tenants
        lab = _seed(migrated_db, tenant_id)
        response = _post(TestClient(create_app()), lab, synth_audio(seconds=30, snr_db=25, silence_ratio=0.05))
        assert response.status_code == 201, response.text
        job_id = uuid.UUID(response.json()["pipeline_job_id"])
        recording_id = uuid.UUID(response.json()["recording_id"])
        assert _status(migrated_db, job_id, tenant_id).status == JobStatus.QUEUED, "the upload returns before any transcription"

        assert asyncio.run(Worker(kinds=["run_pipeline"], url=migrated_db).run_once()) >= 1
        job = _status(migrated_db, job_id, tenant_id)
        assert job.status == JobStatus.SUCCEEDED, job.last_error
        with tenant_session(tenant_id, url=migrated_db) as session:
            run = session.execute(select(PipelineRun).where(PipelineRun.recording_id == recording_id)).scalar_one()
            assert run.status == RunStatus.SUCCEEDED
            assert job.result["pipeline_run_id"] == str(run.id)
            topics = session.execute(text("SELECT topic FROM outbox_event WHERE tenant_id = :t AND payload->>'recording_id' = :r"), {"t": tenant_id, "r": str(recording_id)}).scalars().all()
            assert "recording.ingested" in topics, "the upload announced itself in the same transaction"
    finally:
        runner.set_graph_factory(None)


def _seed(db: str, tenant_id: uuid.UUID) -> dict:
    from radreport.core.types import UserRole
    from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
    from radreport.devtools.synthetic import synth_patient_fields

    with tenant_session(tenant_id, url=db) as session:
        user = AppUser(tenant_id=tenant_id, employee_code=f"E-{uuid.uuid4().hex[:6]}", display_name="Dr Queue", roles=[UserRole.RADIOLOGIST])
        session.add(user)
        session.flush()
        radiologist = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
        patient = Patient(tenant_id=tenant_id, **synth_patient_fields(seed=12))
        session.add_all([radiologist, patient])
        session.flush()
        study = Study(tenant_id=tenant_id, patient_id=patient.id)
        session.add(study)
        session.flush()
        return {"tenant_id": str(tenant_id), "user_id": str(user.id), "study_id": str(study.id), "radiologist_id": str(radiologist.id)}
