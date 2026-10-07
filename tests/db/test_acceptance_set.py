"""Filling and freezing a lab's acceptance set from the admin panel, so the pilot gate can pass."""

from __future__ import annotations

import uuid

import pytest

from radreport.admin import onboarding_steps
from radreport.admin.onboarding_steps import StepRefused
from radreport.core.types import AudioFormat, CaptureDeviceClass, UserRole, VerbatimSource
from radreport.db.models.adaptation import VerbatimTranscript
from radreport.db.models.evaluation import EvalSet
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_patient_fields
from radreport.eval import goldset
from radreport.onboarding.readiness import check_gold_set_frozen, load_facts

pytestmark = pytest.mark.db


def _transcripts(db: str, tenant_id: uuid.UUID, count: int, *, device_class: str = CaptureDeviceClass.DICTATION_MIC_PTT) -> None:
    """`count` annotated recordings spread over three radiologists."""
    with tenant_session(tenant_id, url=db) as session:
        annotator = AppUser(tenant_id=tenant_id, employee_code=f"T-{uuid.uuid4().hex[:6]}", display_name="Annotator", roles=[UserRole.TRANSCRIPTIONIST])
        session.add(annotator)
        session.flush()
        profiles = []
        for i in range(3):
            user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name=f"Dr {i}", roles=[UserRole.RADIOLOGIST])
            session.add(user)
            session.flush()
            profiles.append(RadiologistProfile(tenant_id=tenant_id, user_id=user.id))
        session.add_all(profiles)
        session.flush()
        for index in range(count):
            patient = Patient(tenant_id=tenant_id, **synth_patient_fields(seed=index))
            session.add(patient)
            session.flush()
            study = Study(tenant_id=tenant_id, patient_id=patient.id)
            session.add(study)
            session.flush()
            recording = Recording(tenant_id=tenant_id, study_id=study.id, radiologist_id=profiles[index % 3].id, object_key=f"rec/{uuid.uuid4().hex}.flac", content_hash=uuid.uuid4().hex, duration_seconds=120, measured_snr_db=22.0, capture_device_class=device_class, audio_format=AudioFormat.FLAC)
            session.add(recording)
            session.flush()
            session.add(VerbatimTranscript(tenant_id=tenant_id, recording_id=recording.id, text="the lungs are clear", source=VerbatimSource.HUMAN_ANNOTATION, annotator_id=annotator.id, includes_disfluencies=True, audio_duration_seconds=120, capture_device_class=device_class))


def test_assembling_and_freezing_lets_the_gold_set_check_pass(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    _transcripts(migrated_db, tenant_id, goldset.ACCEPTANCE_TARGET + 5)

    with tenant_session(tenant_id, url=migrated_db) as session:
        assert check_gold_set_frozen(load_facts(session, tenant_id)).status == "fail"
        assembled = onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")
        assert assembled["items"] == goldset.ACCEPTANCE_TARGET and assembled["short_by"] == 0
        onboarding_steps.run_step(session, tenant_id, "acceptance-freeze")
        assert check_gold_set_frozen(load_facts(session, tenant_id)).status == "pass"

        # Frozen means frozen: neither step can change it now.
        with pytest.raises(StepRefused, match="already frozen"):
            onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")


def test_a_short_set_reports_the_gap_and_will_not_freeze(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    _transcripts(migrated_db, tenant_id, 10)

    with tenant_session(tenant_id, url=migrated_db) as session:
        assembled = onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")
        assert assembled["short_by"] == goldset.ACCEPTANCE_TARGET - 10
        with pytest.raises(StepRefused, match="below the floor"):
            onboarding_steps.run_step(session, tenant_id, "acceptance-freeze")


def test_archive_audio_never_fills_the_acceptance_set(migrated_db: str, two_tenants) -> None:
    """The pilot question is about the lab's new hardware; legacy recordings cannot answer it."""
    tenant_id, _ = two_tenants
    _transcripts(migrated_db, tenant_id, 50, device_class=CaptureDeviceClass.LEGACY)

    with tenant_session(tenant_id, url=migrated_db) as session:
        assert onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")["items"] == 0


def test_a_lab_registered_before_this_existed_gets_its_set_created(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert session.query(EvalSet).filter_by(tenant_id=tenant_id).count() == 0
        onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")
        assert session.query(EvalSet).filter_by(tenant_id=tenant_id, is_canonical=False).count() == 1
