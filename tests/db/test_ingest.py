"""The capture path end to end: upload, validate, store, record."""

from __future__ import annotations

import uuid

import pytest

from radreport.adapters.storage.object_store import InMemoryObjectStore
from radreport.core.errors import DuplicateRecording, IngestRejected
from radreport.core.types import CaptureDeviceClass, UserRole
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.orchestration import AuditLog
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_audio, synth_lossy_audio, synth_patient_fields
from radreport.ingest.service import IngestRequest, force_legacy_device_class, ingest_recording

pytestmark = pytest.mark.db


@pytest.fixture
def study_fixture(migrated_db: str, two_tenants):
    """A tenant with one patient, one study and one radiologist."""
    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        user = AppUser(tenant_id=tenant_id, employee_code=f"E-{uuid.uuid4().hex[:6]}", display_name="Dr Test", roles=[UserRole.RADIOLOGIST])
        session.add(user)
        session.flush()

        radiologist = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
        patient = Patient(tenant_id=tenant_id, **synth_patient_fields(seed=1))
        session.add_all([radiologist, patient])
        session.flush()

        study = Study(tenant_id=tenant_id, patient_id=patient.id)
        session.add(study)
        session.flush()
        return tenant_id, study.id, radiologist.id


def _request(tenant_id, study_id, radiologist_id, data: bytes, **kwargs) -> IngestRequest:
    return IngestRequest(tenant_id=tenant_id, study_id=study_id, radiologist_id=radiologist_id, filename="dictation.flac", data=data, **kwargs)


def test_a_recording_is_stored_validated_and_audited(migrated_db: str, study_fixture) -> None:
    """The capture path's exit criterion, stated as a test: a recording can be uploaded, validated, stored and audited. Nothing interprets it yet."""
    tenant_id, study_id, radiologist_id = study_fixture
    store = InMemoryObjectStore()
    audio = synth_audio(seconds=30, snr_db=25, silence_ratio=0.05)

    with tenant_session(tenant_id, url=migrated_db) as session:
        result = ingest_recording(session, store, _request(tenant_id, study_id, radiologist_id, audio))
        recording = result.recording

        assert store.exists(recording.object_key)
        assert recording.audio_format == "flac"
        assert recording.measured_snr_db is not None
        assert recording.silence_ratio is not None
        # never set at ingest, always derived.
        assert recording.is_training_corpus_eligible is False

        audits = session.query(AuditLog).filter(AuditLog.entity_id == recording.id, AuditLog.action == "recording_ingested").all()
        assert len(audits) == 1


def test_a_retried_upload_is_a_no_op(migrated_db: str, study_fixture) -> None:
    """`content_hash` is "the cheapest possible protection against double-processing, which will otherwise happen the first time someone retries a failed upload"."""
    tenant_id, study_id, radiologist_id = study_fixture
    store = InMemoryObjectStore()
    audio = synth_audio(seconds=25, seed=7)

    with tenant_session(tenant_id, url=migrated_db) as session:
        first = ingest_recording(session, store, _request(tenant_id, study_id, radiologist_id, audio))

    with tenant_session(tenant_id, url=migrated_db) as session:
        with pytest.raises(DuplicateRecording) as exc:
            ingest_recording(session, store, _request(tenant_id, study_id, radiologist_id, audio))
    assert exc.value.recording_id == str(first.recording.id)


def test_a_duplicate_does_not_write_to_object_storage(migrated_db: str, study_fixture) -> None:
    """Dedup happens before the store write, so a retry is genuinely free rather than "free except for the upload"."""
    tenant_id, study_id, radiologist_id = study_fixture
    store = InMemoryObjectStore()
    audio = synth_audio(seconds=25, seed=8)

    with tenant_session(tenant_id, url=migrated_db) as session:
        ingest_recording(session, store, _request(tenant_id, study_id, radiologist_id, audio))
    stored_after_first = len(store._data)

    with tenant_session(tenant_id, url=migrated_db) as session:
        with pytest.raises(DuplicateRecording):
            ingest_recording(session, store, _request(tenant_id, study_id, radiologist_id, audio))

    assert len(store._data) == stored_after_first


def test_lossy_audio_never_reaches_storage(migrated_db: str, study_fixture) -> None:
    """The rejection, proven at the boundary rather than in the gate alone."""
    tenant_id, study_id, radiologist_id = study_fixture
    store = InMemoryObjectStore()

    with tenant_session(tenant_id, url=migrated_db) as session:
        with pytest.raises(IngestRejected) as exc:
            ingest_recording(session, store, _request(tenant_id, study_id, radiologist_id, synth_lossy_audio()))
    assert exc.value.code == "lossy_codec"
    assert not store._data


def test_a_study_from_another_tenant_is_refused(migrated_db: str, study_fixture, two_tenants) -> None:
    """The composite FKs would catch this at COMMIT; catching it here produces an error that names the problem rather than a constraint violation."""
    _tenant_a, tenant_b = two_tenants
    _tid, study_id, radiologist_id = study_fixture

    with tenant_session(tenant_b, url=migrated_db) as session:
        with pytest.raises(ValueError, match="not in tenant"):
            ingest_recording(session, InMemoryObjectStore(), _request(tenant_b, study_id, radiologist_id, synth_audio(seconds=20)))


def test_lab_admin_uploads_are_forced_to_the_legacy_partition() -> None:
    """Lab-admin uploads are forced to the `legacy` partition by the importer, not trusted from the label."""
    original = IngestRequest(tenant_id=uuid.uuid4(), study_id=uuid.uuid4(), radiologist_id=uuid.uuid4(), filename="archive.wav", data=b"", capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT, is_push_to_talk=True)
    forced = force_legacy_device_class(original)
    assert forced.capture_device_class == CaptureDeviceClass.LEGACY
    assert forced.is_push_to_talk is False
