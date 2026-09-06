"""Builds a signed report end to end, for tests that need one to already exist."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.orm import Session

from radreport.core.types import AssertionStatus, AudioFormat, AutonomyStatus, CaptureDeviceClass, DraftStatus, FillSource, Laterality, UserRole
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import AutonomyClass, Template, TemplateField, TemplateVersion
from radreport.db.models.reporting import ReportDraft, ReportFieldValue
from radreport.knowledge.phonetics import double_metaphone
from radreport.review import session as review_session
from radreport.review import signing
from radreport.review.rbac import Reviewer


def build_signed_report(session: Session, tenant_id: uuid.UUID, *, with_autonomy_class: bool = False) -> dict[str, Any]:
    """Create and sign one report, returning the ids a test needs."""
    radiologist_user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Rad", roles=[UserRole.RADIOLOGIST])
    session.add(radiologist_user)
    session.flush()

    profile = RadiologistProfile(tenant_id=tenant_id, user_id=radiologist_user.id)
    patient = Patient(tenant_id=tenant_id, mrn=f"M{uuid.uuid4().hex[:8]}", pseudonym=f"P{uuid.uuid4().hex[:8]}")
    session.add_all([profile, patient])
    session.flush()

    study = Study(tenant_id=tenant_id, patient_id=patient.id)
    session.add(study)
    session.flush()

    recording = Recording(tenant_id=tenant_id, study_id=study.id, radiologist_id=profile.id, object_key=f"rec/{uuid.uuid4().hex}.flac", content_hash=uuid.uuid4().hex, duration_seconds=120, capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT, audio_format=AudioFormat.FLAC)
    session.add(recording)
    session.flush()

    autonomy_class_id = None
    if with_autonomy_class:
        klass = AutonomyClass(tenant_id=tenant_id, code=f"CLASS-{uuid.uuid4().hex[:6]}", display_name="Abdominal ultrasound", status=AutonomyStatus.ACCRUING, baseline_cse_rate=0.025, ni_margin_pp=1.0, required_n=100, cusum_threshold=3.0)
        session.add(klass)
        session.flush()
        autonomy_class_id = klass.id

    template = Template(tenant_id=tenant_id, code=f"T-{uuid.uuid4().hex[:6]}", display_name="USG Abdomen", modality="US", body_region="abdomen", autonomy_class_id=autonomy_class_id)
    session.add(template)
    session.flush()

    version = TemplateVersion(tenant_id=tenant_id, template_id=template.id, version=1, json_schema={}, render_spec={}, routing_card="usg abdomen", trigger_rules={}, spoken_study_code=f"usg abdomen {uuid.uuid4().hex[:4]}", spoken_study_code_phonetic=double_metaphone("usg abdomen")[0], effective_from=dt.datetime.now(dt.UTC), is_current=True)
    session.add(version)
    session.flush()

    field = TemplateField(tenant_id=tenant_id, template_version_id=version.id, field_key="liver", section="FINDINGS", display_label="Liver", data_type="text", seq=1)
    session.add(field)
    session.flush()

    draft = ReportDraft(tenant_id=tenant_id, recording_id=recording.id, template_version_id=version.id, rendered_text="FINDINGS\nLiver: normal", structured_payload={}, overall_confidence=0.9, flagged_field_count=0, prompt_bundle_version="v1", model_versions={}, status=DraftStatus.GENERATED)
    session.add(draft)
    session.flush()

    session.add(ReportFieldValue(tenant_id=tenant_id, report_draft_id=draft.id, template_field_id=field.id, value_text="normal", assertion_status=AssertionStatus.PRESENT, laterality=Laterality.NA, fill_source=FillSource.DICTATED, is_grounded=True, confidence=0.95, is_flagged=False))
    session.flush()

    reviewer = Reviewer(user_id=radiologist_user.id, roles=(UserRole.RADIOLOGIST,))
    review_session.record_revision(session, tenant_id=tenant_id, draft_id=draft.id, reviewer=reviewer, edits=[], active_edit_seconds=30, wall_clock_seconds=60, rendered_text="FINDINGS\nLiver: normal")
    final = signing.sign_report(session, tenant_id=tenant_id, draft_id=draft.id, reviewer=reviewer)

    return {"final_report_id": final.id, "draft_id": draft.id, "recording_id": recording.id, "study_id": study.id, "autonomy_class_id": autonomy_class_id, "radiologist": reviewer}
