"""Granting and withdrawing the right to skip review, plus the training prerequisites.

Granting is deliberate and hard; withdrawing is mechanical and easy.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from radreport.adaptation.gates import AdaptationBlocked, evaluate_gates, require_gates
from radreport.autonomy import accrual, grant
from radreport.autonomy.grant import GrantRefused
from radreport.core.types import AdaptationTarget, AudioFormat, AutonomyStatus, CaptureDeviceClass, SeverityGrade, UserRole, VerbatimSource
from radreport.db.models.adaptation import VerbatimTranscript
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import AutonomyClass
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_patient_fields

pytestmark = pytest.mark.db


@pytest.fixture
def autonomy_fixture(migrated_db: str, two_tenants):
    """A class with a measured baseline, accruing."""
    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        klass = AutonomyClass(tenant_id=tenant_id, code="ABDO_US", display_name="Abdominal ultrasound", status=AutonomyStatus.NOT_EVALUATED, baseline_cse_rate=0.025, ni_margin_pp=1.0, required_n=100, cusum_threshold=grant.DEFAULT_CUSUM_THRESHOLD)
        session.add(klass)
        session.flush()
        return {"db": migrated_db, "tenant_id": tenant_id, "class_id": klass.id}


def test_accrual_cannot_open_without_a_measured_baseline(migrated_db: str, two_tenants) -> None:
    """'s audit produces `baseline_cse_rate`. Accruing against an assumed number computes a posterior about something nobody measured."""
    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        session.add(AutonomyClass(tenant_id=tenant_id, code="NO_BASELINE", display_name="No baseline", status=AutonomyStatus.NOT_EVALUATED, baseline_cse_rate=0.0, required_n=100, cusum_threshold=3.0))
        session.flush()
        with pytest.raises(ValueError, match="baseline"):
            accrual.open_accrual(session, tenant_id=tenant_id, class_code="NO_BASELINE")


def test_a_grant_is_refused_on_insufficient_volume(autonomy_fixture) -> None:
    """A posterior from too few observations is confident about the wrong thing."""
    f = autonomy_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        accrual.open_accrual(session, tenant_id=f["tenant_id"], class_code="ABDO_US")
        with pytest.raises(GrantRefused) as exc:
            grant.grant(session, tenant_id=f["tenant_id"], class_code="ABDO_US", platform_user_id=uuid.uuid4())
        assert exc.value.code == "insufficient_volume"


def test_revocation_is_mechanical_and_needs_no_preconditions(autonomy_fixture) -> None:
    """Anything that makes revocation harder than granting has the asymmetry backwards."""
    f = autonomy_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        klass = grant.revoke(session, tenant_id=f["tenant_id"], class_code="ABDO_US", reason="operator decision")
        assert klass.status == AutonomyStatus.REVOKED
        assert klass.revocation_reason == "operator decision"
        assert klass.revoked_at is not None


def test_a_revoked_class_cannot_simply_be_re_granted(autonomy_fixture) -> None:
    """Re-granting the evidence that was already judged is not a new judgement; it needs a fresh accrual period."""
    f = autonomy_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        grant.revoke(session, tenant_id=f["tenant_id"], class_code="ABDO_US", reason="degraded")
        with pytest.raises(GrantRefused) as exc:
            grant.grant(session, tenant_id=f["tenant_id"], class_code="ABDO_US", platform_user_id=uuid.uuid4())
        assert exc.value.code == "previously_revoked"


def test_a_granted_class_is_revoked_when_the_cusum_signals(autonomy_fixture) -> None:
    """The mechanical path, end to end: grades go in, autonomy comes off, and the statistic that caused it is in the record."""
    f = autonomy_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        klass = session.get(AutonomyClass, f["class_id"])
        klass.status = AutonomyStatus.GRANTED
        klass.granted_at = dt.datetime.now(dt.UTC)
        klass.cusum_statistic = 0.0
        session.flush()

        # A burst of clinically significant errors.
        signalled = False
        for _ in range(8):
            step = grant.observe_graded_report(session, tenant_id=f["tenant_id"], class_code="ABDO_US", severity_grade=SeverityGrade.G4)
            if step.signalled:
                signalled = True
                break

        assert signalled
        session.refresh(klass)
        assert klass.status == AutonomyStatus.REVOKED
        assert "CUSUM" in klass.revocation_reason


def test_clean_reports_do_not_revoke_a_granted_class(autonomy_fixture) -> None:
    f = autonomy_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        klass = session.get(AutonomyClass, f["class_id"])
        klass.status = AutonomyStatus.GRANTED
        klass.cusum_statistic = 0.0
        session.flush()

        for _ in range(100):
            grant.observe_graded_report(session, tenant_id=f["tenant_id"], class_code="ABDO_US", severity_grade=SeverityGrade.G0)

        session.refresh(klass)
        assert klass.status == AutonomyStatus.GRANTED
        assert float(klass.cusum_statistic) == 0.0


def test_grading_a_report_feeds_accrual_and_the_monitor(migrated_db: str, two_tenants) -> None:
    """The loop depends on, closed end to end."""
    from tests.db.review_factory import build_signed_report

    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        setup = build_signed_report(session, tenant_id, with_autonomy_class=True)
        grant_module = grant

        klass = session.get(AutonomyClass, setup["autonomy_class_id"])
        klass.status = AutonomyStatus.GRANTED
        klass.cusum_statistic = 0.0
        session.flush()

        from radreport.review import grading as review_grading

        result = review_grading.grade_report(session, tenant_id=tenant_id, final_report_id=setup["final_report_id"], grade=SeverityGrade.G4, reviewer=setup["radiologist"])

        assert result.is_cse is True
        assert result.accrued is True, "a graded report must become autonomy evidence"

        session.refresh(klass)
        assert klass.accrued_n == 1
        assert klass.observed_cse_count == 1
        # And the monitor moved.
        assert float(klass.cusum_statistic) > 0.0
        assert grant_module is grant


def test_a_regrade_corrects_the_evidence_rather_than_adding_to_it(migrated_db: str, two_tenants) -> None:
    """Counting a regraded report twice would let a disputed grade move the CSE rate more than an undisputed one."""
    from tests.db.review_factory import build_signed_report

    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        setup = build_signed_report(session, tenant_id, with_autonomy_class=True)
        klass = session.get(AutonomyClass, setup["autonomy_class_id"])
        klass.status = AutonomyStatus.ACCRUING
        session.flush()

        from radreport.review import grading as review_grading

        review_grading.grade_report(session, tenant_id=tenant_id, final_report_id=setup["final_report_id"], grade=SeverityGrade.G4, reviewer=setup["radiologist"])
        session.refresh(klass)
        assert (klass.accrued_n, klass.observed_cse_count) == (1, 1)

        review_grading.grade_report(session, tenant_id=tenant_id, final_report_id=setup["final_report_id"], grade=SeverityGrade.G1, reviewer=setup["radiologist"])
        session.refresh(klass)
        # One report, still one observation, and the CSE count corrected down.
        assert klass.accrued_n == 1
        assert klass.observed_cse_count == 0


def test_an_eval_set_report_is_graded_but_does_not_accrue(migrated_db: str, two_tenants) -> None:
    """applied to autonomy evidence: an item that tuned or evaluated the pipeline cannot also be evidence that it works."""
    from radreport.db.models.evaluation import EvalItem, EvalSet
    from tests.db.review_factory import build_signed_report

    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        setup = build_signed_report(session, tenant_id, with_autonomy_class=True)
        klass = session.get(AutonomyClass, setup["autonomy_class_id"])
        klass.status = AutonomyStatus.ACCRUING
        session.flush()

        eval_set = EvalSet(tenant_id=tenant_id, name=f"acc-{uuid.uuid4().hex[:8]}")
        session.add(eval_set)
        session.flush()
        session.add(EvalItem(tenant_id=tenant_id, eval_set_id=eval_set.id, recording_id=setup["recording_id"], gold_transcript_verbatim="x", capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT))
        session.flush()

        from radreport.review import grading as review_grading

        result = review_grading.grade_report(session, tenant_id=tenant_id, final_report_id=setup["final_report_id"], grade=SeverityGrade.G4, reviewer=setup["radiologist"])

        # The grade is recorded; it just does not move the posterior.
        assert result.accrued is False
        session.refresh(klass)
        assert klass.accrued_n == 0
        assert klass.observed_cse_count == 0


def test_an_accruing_class_is_not_monitored(autonomy_fixture) -> None:
    """Only a granted class is monitored; an accruing one is gathering evidence for a decision nobody has made."""
    f = autonomy_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        accrual.open_accrual(session, tenant_id=f["tenant_id"], class_code="ABDO_US")
        step = grant.observe_graded_report(session, tenant_id=f["tenant_id"], class_code="ABDO_US", severity_grade=SeverityGrade.G4)
        assert step.signalled is False
        assert step.increment == 0.0


# ============================================== adaptation gates ======
@pytest.fixture
def corpus_fixture(migrated_db: str, two_tenants):
    """Verbatim hours across several speakers on one capture class."""
    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        annotator = AppUser(tenant_id=tenant_id, employee_code=f"T-{uuid.uuid4().hex[:6]}", display_name="Asha", roles=[UserRole.TRANSCRIPTIONIST])
        session.add(annotator)
        session.flush()

        recording_ids: list[uuid.UUID] = []
        for speaker_index in range(6):
            user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name=f"Dr {speaker_index}", roles=[UserRole.RADIOLOGIST])
            session.add(user)
            session.flush()
            profile = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
            session.add(profile)
            session.flush()

            for _ in range(2):
                patient = Patient(tenant_id=tenant_id, **synth_patient_fields(seed=len(recording_ids)))
                session.add(patient)
                session.flush()
                study = Study(tenant_id=tenant_id, patient_id=patient.id)
                session.add(study)
                session.flush()
                recording = Recording(
                    tenant_id=tenant_id,
                    study_id=study.id,
                    radiologist_id=profile.id,
                    object_key=f"rec/{uuid.uuid4().hex}.flac",
                    content_hash=uuid.uuid4().hex,
                    duration_seconds=3600,  # 1 h each
                    capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT,
                    audio_format=AudioFormat.FLAC,
                )
                session.add(recording)
                session.flush()
                session.add(VerbatimTranscript(tenant_id=tenant_id, recording_id=recording.id, text="verbatim with um disfluencies retained", source=VerbatimSource.HUMAN_ANNOTATION, annotator_id=annotator.id, includes_disfluencies=True, audio_duration_seconds=3600, capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT))
                recording_ids.append(recording.id)
        session.flush()
        return {"db": migrated_db, "tenant_id": tenant_id, "recording_ids": recording_ids}


def test_the_two_unspecified_gates_fail_closed(corpus_fixture) -> None:
    """The G2 and G5 are not stated in PLAN.md and were not recoverable from the design PDF."""
    f = corpus_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        report = evaluate_gates(session, recording_ids=f["recording_ids"])

        unimplemented = [r for r in report.failures if "not_implemented" in r.gate_id]
        assert len(unimplemented) == 2
        assert report.passed is False
        assert all("not implemented" in r.reason for r in unimplemented)

        with pytest.raises(AdaptationBlocked):
            require_gates(session, recording_ids=f["recording_ids"])


def test_volume_and_speaker_balance_pass_on_a_good_corpus(corpus_fixture) -> None:
    """12 h across 6 balanced speakers on one capture class."""
    f = corpus_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        report = evaluate_gates(session, recording_ids=f["recording_ids"])
        by_id = {r.gate_id: r for r in report.results}

        assert by_id["G3_speaker_balance"].passed
        assert by_id["G3_speaker_balance"].measured["speakers"] == 6
        assert by_id["G4_hardware_homogeneous"].passed
        # 12 h is short of the 20 h global floor.
        assert by_id["G1_volume"].passed is False
        assert by_id["G1_volume"].measured["hours"] == pytest.approx(12.0)


def test_speaker_balance_does_not_apply_to_a_per_speaker_adapter(corpus_fixture) -> None:
    """The fallback: a per-speaker adapter needs only 5 h and the radiologist's own consent — no pooling at all."""
    f = corpus_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        report = evaluate_gates(session, recording_ids=f["recording_ids"], target=AdaptationTarget.ASR_SPEAKER)
        by_id = {r.gate_id: r for r in report.results}

        assert by_id["G3_speaker_balance"].passed
        assert by_id["G3_speaker_balance"].measured["applicable"] is False
        # And the volume floor drops from 20 h to 5 h.
        assert by_id["G1_volume"].passed is True


def test_a_cleaned_transcript_does_not_count_toward_the_volume_gate(corpus_fixture) -> None:
    """A cleaned-up transcript teaches an ASR model to delete words, so counting its hours would clear the gate on harmful material."""
    f = corpus_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        for transcript in session.query(VerbatimTranscript).filter(VerbatimTranscript.tenant_id == f["tenant_id"]).limit(6).all():
            transcript.includes_disfluencies = False
        session.flush()

        report = evaluate_gates(session, recording_ids=f["recording_ids"])
        volume = next(r for r in report.results if r.gate_id == "G1_volume")
        assert volume.measured["hours"] == pytest.approx(6.0)
        assert volume.measured["excluded_items"] == 6


def test_mixed_capture_hardware_fails_the_homogeneity_gate(corpus_fixture) -> None:
    """`legacy` and `current` are different distributions, so an adapter trained across both fits neither."""
    f = corpus_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        for transcript in session.query(VerbatimTranscript).filter(VerbatimTranscript.tenant_id == f["tenant_id"]).limit(5).all():
            transcript.capture_device_class = CaptureDeviceClass.LEGACY
        session.flush()

        report = evaluate_gates(session, recording_ids=f["recording_ids"])
        gate = next(r for r in report.results if r.gate_id == "G4_hardware_homogeneous")
        assert gate.passed is False
        assert gate.measured["legacy_hours"] == pytest.approx(5.0)


def test_the_gate_report_serialises_for_the_adaptation_run(corpus_fixture) -> None:
    """`model_adaptation_run.prerequisite_gates_passed` must record which condition permitted a training run."""
    f = corpus_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        payload = evaluate_gates(session, recording_ids=f["recording_ids"]).as_json()

        assert payload["all_passed"] is False
        assert len(payload["gates"]) == 6
        assert all("reason" in g for g in payload["gates"].values())
