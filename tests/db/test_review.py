"""Review through to a signed report.

The steps between a draft and a legal medical record are refusals, so these are mostly tests
that the right things are refused.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from radreport.core.types import AlertSeverity, AssertionStatus, AudioFormat, CaptureDeviceClass, CheckType, DraftStatus, ErrorCategory, FillSource, HumanVerdict, Laterality, PathType, PatternType, Severity, SeverityGrade, StudyPriority, UserRole
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import Template, TemplateField, TemplateVersion
from radreport.db.models.reporting import CriticalFindingAlert, CriticalFindingRule, ProvenanceSpan, ReportDraft, ReportFieldValue, VerificationFinding
from radreport.db.models.review import EditEvent
from radreport.db.session import tenant_session
from radreport.knowledge.phonetics import double_metaphone
from radreport.review import feedback, grading, signing
from radreport.review import queue as review_queue
from radreport.review import session as review_session
from radreport.review.rbac import PermissionDenied, Reviewer
from radreport.review.signing import SigningRefused

pytestmark = pytest.mark.db


@pytest.fixture
def review_fixture(migrated_db: str, two_tenants):
    """A generated draft with one grounded field and one flagged field."""
    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        radiologist_user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Rad", roles=[UserRole.RADIOLOGIST])
        assistant_user = AppUser(tenant_id=tenant_id, employee_code=f"T-{uuid.uuid4().hex[:6]}", display_name="Asha", roles=[UserRole.TRANSCRIPTIONIST])
        session.add_all([radiologist_user, assistant_user])
        session.flush()

        profile = RadiologistProfile(tenant_id=tenant_id, user_id=radiologist_user.id)
        patient = Patient(tenant_id=tenant_id, mrn=f"M{uuid.uuid4().hex[:8]}", pseudonym=f"P{uuid.uuid4().hex[:8]}")
        session.add_all([profile, patient])
        session.flush()
        study = Study(tenant_id=tenant_id, patient_id=patient.id, priority=StudyPriority.ROUTINE)
        session.add(study)
        session.flush()
        recording = Recording(tenant_id=tenant_id, study_id=study.id, radiologist_id=profile.id, object_key=f"rec/{uuid.uuid4().hex}.flac", content_hash=uuid.uuid4().hex, duration_seconds=120, capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT, audio_format=AudioFormat.FLAC)
        session.add(recording)
        session.flush()

        template = Template(tenant_id=tenant_id, code="USG_ABDOMEN", display_name="USG Abdomen", modality="US", body_region="abdomen")
        session.add(template)
        session.flush()
        version = TemplateVersion(tenant_id=tenant_id, template_id=template.id, version=1, json_schema={}, render_spec={}, routing_card="usg abdomen", trigger_rules={}, spoken_study_code="usg abdomen", spoken_study_code_phonetic=double_metaphone("usg abdomen")[0], effective_from=dt.datetime.now(dt.UTC), is_current=True)
        session.add(version)
        session.flush()

        liver = TemplateField(tenant_id=tenant_id, template_version_id=version.id, field_key="liver", section="FINDINGS", display_label="Liver", data_type="text", seq=1)
        kidney = TemplateField(tenant_id=tenant_id, template_version_id=version.id, field_key="kidney", section="FINDINGS", display_label="Kidney", data_type="text", seq=2, is_critical=True)
        session.add_all([liver, kidney])
        session.flush()

        transcript_text = "the liver is normal. a cyst in the left kidney."
        from radreport.core.types import TranscriptStage
        from radreport.db.models.asr import Transcript

        transcript = Transcript(tenant_id=tenant_id, recording_id=recording.id, version=1, stage=TranscriptStage.RAW, text=transcript_text, source_asr_run_ids=[], is_current=True)
        session.add(transcript)
        session.flush()

        draft = ReportDraft(tenant_id=tenant_id, recording_id=recording.id, template_version_id=version.id, rendered_text="FINDINGS\nLiver: normal", structured_payload={}, overall_confidence=0.82, flagged_field_count=1, prompt_bundle_version="v1", model_versions={}, status=DraftStatus.GENERATED)
        session.add(draft)
        session.flush()

        liver_value = ReportFieldValue(tenant_id=tenant_id, report_draft_id=draft.id, template_field_id=liver.id, value_text="normal", assertion_status=AssertionStatus.PRESENT, laterality=Laterality.NA, fill_source=FillSource.DICTATED, is_grounded=True, confidence=0.95, is_flagged=False)
        kidney_value = ReportFieldValue(tenant_id=tenant_id, report_draft_id=draft.id, template_field_id=kidney.id, value_text="a cyst in the left kidney", assertion_status=AssertionStatus.PRESENT, laterality=Laterality.LEFT, fill_source=FillSource.DICTATED, is_grounded=True, confidence=0.55, is_flagged=True, flag_reasons=["sample_disagreement"])
        session.add_all([liver_value, kidney_value])
        session.flush()

        quote = "a cyst in the left kidney"
        session.add(ProvenanceSpan(tenant_id=tenant_id, report_field_value_id=kidney_value.id, transcript_id=transcript.id, char_start=transcript_text.index(quote), char_end=transcript_text.index(quote) + len(quote), audio_start_ms=4200, audio_end_ms=7100))
        session.flush()

        return {"db": migrated_db, "tenant_id": tenant_id, "draft_id": draft.id, "recording_id": recording.id, "kidney_value_id": kidney_value.id, "liver_value_id": liver_value.id, "radiologist": Reviewer(user_id=radiologist_user.id, roles=(UserRole.RADIOLOGIST,)), "assistant": Reviewer(user_id=assistant_user.id, roles=(UserRole.TRANSCRIPTIONIST,))}


def test_the_draft_opens_with_flagged_and_critical_fields_first(review_fixture) -> None:
    """A reviewer's attention runs out, so spend it where a mistake is a G4."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        view = review_session.open_draft(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])
        assert [x.field_key for x in view.fields] == ["kidney", "liver"]
        assert view.fields[0].is_flagged and view.fields[0].is_critical
        # Opening claims the draft so two reviewers do not work it at once.
        assert view.status == DraftStatus.IN_REVIEW
        # Click-to-listen needs the audio offsets.
        assert view.fields[0].provenance[0]["audio_start_ms"] == 4200


def test_active_edit_seconds_is_clamped_to_the_wall_clock(review_fixture) -> None:
    """The commercial argument rests on this number, and it arrives from a browser."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        result = review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["assistant"], edits=[], active_edit_seconds=9999, wall_clock_seconds=42, rendered_text="FINDINGS\nLiver: normal")
        assert result.active_edit_seconds == 42
        assert result.clamped is True
        assert result.revision.active_edit_seconds == 42


def test_an_edit_produces_a_categorised_training_row(review_fixture) -> None:
    """Every `asr_term` row with an audio span is one training example."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        result = review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["assistant"], edits=[review_session.FieldEdit(field_value_id=f["kidney_value_id"], value_text="a cyst in the right kidney")], active_edit_seconds=30, wall_clock_seconds=60, rendered_text="FINDINGS\nKidney: right")
        assert len(result.edit_events) == 1
        event = result.edit_events[0]
        assert event.error_category == ErrorCategory.LATERALITY
        # The audio span is what makes this a training example rather than a
        # note that something changed.
        assert event.audio_start_ms == 4200
        assert event.is_training_eligible is True

        # A reviewer's correction is ground truth from here on.
        value = session.get(ReportFieldValue, f["kidney_value_id"])
        assert value.fill_source == FillSource.HUMAN
        assert value.is_flagged is False


def test_an_assistant_cannot_sign(review_fixture) -> None:
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["assistant"], edits=[], active_edit_seconds=10, wall_clock_seconds=20, rendered_text="x")
        with pytest.raises(PermissionDenied):
            signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["assistant"])


def test_signing_is_refused_without_a_review(review_fixture) -> None:
    """A report must be reviewed before it is signed, even when nothing needed changing."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        with pytest.raises(SigningRefused, match="no revision"):
            signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])


def test_a_blocking_finding_refuses_the_signature(review_fixture) -> None:
    """The `block` means "must not reach a queue"; signing is further."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        session.add(VerificationFinding(tenant_id=f["tenant_id"], report_draft_id=f["draft_id"], check_id="laterality_disagrees_with_source", check_type=CheckType.RULE, severity=Severity.BLOCK, message="contradiction"))
        session.flush()
        review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"], edits=[], active_edit_seconds=5, wall_clock_seconds=10, rendered_text="x")
        with pytest.raises(SigningRefused, match="blocking"):
            signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])


def test_an_unacknowledged_critical_alert_refuses_the_signature(review_fixture) -> None:
    """The alert bypassed the queue; the acknowledgement is the record that a human received it."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        rule = CriticalFindingRule(tenant_id=f["tenant_id"], code="PNEUMOTHORAX", finding_label="Pneumothorax", pattern_type=PatternType.LEXICAL, pattern="pneumothorax", severity=AlertSeverity.RED, sla_minutes=30, escalation_path=[], is_active=True)
        session.add(rule)
        session.flush()
        alert = CriticalFindingAlert(tenant_id=f["tenant_id"], recording_id=f["recording_id"], rule_id=rule.id, evidence_text="large pneumothorax", confidence=0.95, sla_due_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30))
        session.add(alert)
        session.flush()

        review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"], edits=[], active_edit_seconds=5, wall_clock_seconds=10, rendered_text="x")
        with pytest.raises(SigningRefused, match="acknowledg"):
            signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])

        signing.acknowledge_alert(session, tenant_id=f["tenant_id"], alert_id=alert.id, reviewer=f["radiologist"], outcome=HumanVerdict.TRUE_POSITIVE)
        final = signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])
        assert final.content_hash


def test_a_signed_report_records_which_path_produced_it(review_fixture) -> None:
    """The autonomy audit asks which path produced the report, not who clicked sign — so it is derived from the revision history."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["assistant"], edits=[], active_edit_seconds=20, wall_clock_seconds=40, rendered_text="x")
        final = signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])
        assert final.path_type == PathType.TRANSCRIPTIONIST_REVIEWED
        assert session.get(ReportDraft, f["draft_id"]).status == DraftStatus.SIGNED

        with pytest.raises(SigningRefused, match="already signed"):
            signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])


def test_a_correction_after_signing_is_an_addendum(review_fixture) -> None:
    """A signed report is content-hashed for tamper evidence; editing it would destroy the account of what was communicated at the time."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"], edits=[], active_edit_seconds=5, wall_clock_seconds=10, rendered_text="original text")
        original = signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])
        addendum = signing.amend_report(session, tenant_id=f["tenant_id"], original_report_id=original.id, reviewer=f["radiologist"], rendered_text="corrected text", reason="laterality corrected after review")
        assert addendum.amends_report_id == original.id
        assert addendum.content_hash != original.content_hash
        # The original is untouched.
        assert original.rendered_text == "original text"


def test_grading_is_recorded_and_regrading_keeps_both_values(review_fixture) -> None:
    """A CSE rate that can be quietly adjusted is not evidence."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"], edits=[review_session.FieldEdit(field_value_id=f["kidney_value_id"], value_text="a cyst in the right kidney")], active_edit_seconds=30, wall_clock_seconds=60, rendered_text="x")
        final = signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])

        first = grading.grade_report(session, tenant_id=f["tenant_id"], final_report_id=final.id, grade=SeverityGrade.G4, reviewer=f["radiologist"])
        assert first.is_cse is True
        assert first.previous_grade is None

        second = grading.grade_report(session, tenant_id=f["tenant_id"], final_report_id=final.id, grade=SeverityGrade.G1, reviewer=f["radiologist"])
        assert second.previous_grade == SeverityGrade.G4
        assert second.is_cse is False

        events = session.query(EditEvent).filter(EditEvent.tenant_id == f["tenant_id"]).all()
        assert {e.severity_grade for e in events} == {SeverityGrade.G1}


def test_an_assistant_cannot_grade(review_fixture) -> None:
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        with pytest.raises(PermissionDenied):
            grading.grade_report(session, tenant_id=f["tenant_id"], final_report_id=uuid.uuid4(), grade=SeverityGrade.G0, reviewer=f["assistant"])


def test_the_useless_button_actually_records(review_fixture) -> None:
    """The usefulness button must record something; a button that logs nothing is the failure this guards against."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        feedback.report_usefulness(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["assistant"], was_useless=True, reason="routed to the wrong template")
        stats = feedback.usefulness_stats(session, tenant_id=f["tenant_id"])
        assert (stats.reported, stats.useless) == (1, 1)

        # A reviewer changing their mind is a correction, not a second data point.
        feedback.report_usefulness(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["assistant"], was_useless=False)
        stats = feedback.usefulness_stats(session, tenant_id=f["tenant_id"])
        assert (stats.reported, stats.useless) == (1, 0)


def test_the_queue_hides_radiologist_only_work_from_an_assistant(review_fixture) -> None:
    """A queue that shows an assistant work they cannot complete teaches them to ignore it."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        draft = session.get(ReportDraft, f["draft_id"])
        draft.overall_confidence = 0.40  # below the threshold
        session.flush()

        for_radiologist = review_queue.build_queue(session, tenant_id=f["tenant_id"], reviewer=f["radiologist"])
        for_assistant = review_queue.build_queue(session, tenant_id=f["tenant_id"], reviewer=f["assistant"])
        assert len(for_radiologist) == 1
        assert for_radiologist[0].requires_radiologist is True
        assert for_assistant == []


def test_the_sign_button_state_matches_what_signing_will_do(review_fixture) -> None:
    """A button that looks available while the call behind it refuses is the "click and see" behaviour preflight exists to prevent."""
    f = review_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        before = signing.preflight(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"])
        assert before.has_revision is False
        assert before.may_sign is False  # and sign_report would refuse

        review_session.record_revision(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"], edits=[], active_edit_seconds=5, wall_clock_seconds=10, rendered_text="x")
        after = signing.preflight(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"])
        assert after.may_sign is True
        assert signing.sign_report(session, tenant_id=f["tenant_id"], draft_id=f["draft_id"], reviewer=f["radiologist"])
