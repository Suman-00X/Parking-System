"""Onboarding end to end against a real database, from roster import through to the readiness gate."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.errors import ApprovalRequired, BatchBlocked, ConsentRequired
from radreport.core.types import AbsencePolicy, AudioFormat, CandidateReviewStatus, CaptureDeviceClass, CollisionResolution, CollisionSeverity, ImportStatus, MatchMethod, VerbatimSource
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import Template, TemplateField, TemplateVersion
from radreport.db.models.onboarding import CollisionAuditFinding, CorpusReportTemplateMap
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_patient_fields
from radreport.devtools.synthetic import synth_template_docx as make_docx
from radreport.onboarding import boilerplate, corpus, critical_rules, lexicon, paired_audio, roster, templates
from radreport.onboarding.batches import ArtifactUpload
from radreport.onboarding.readiness import evaluate_readiness

pytestmark = pytest.mark.db


ROSTER_CSV = b"employee_code,display_name,email,roles\nR1,Dr Anand,anand@lab.in,radiologist\nR2,Dr Bhat,bhat@lab.in,radiologist;lab_admin\nT1,Asha Rao,asha@lab.in,transcriptionist\n"

CHEST_CT_DOC = [("CT Chest Plain", True), ("FINDINGS", True), ("Lungs: Normal in size and attenuation", False), ("Pleura: No effusion [present/absent]", False), ("Mediastinum: Largest node 3.2 cm in short axis", False), ("IMPRESSION", True), ("Summary: Unremarkable study", False)]

USG_ABDO_DOC = [("USG Abdomen", True), ("FINDINGS", True), ("Liver: Normal in size and echotexture", False), ("Gallbladder: Unremarkable, no calculi", False), ("Kidneys: Both kidneys are normal in size", False)]


@pytest.fixture
def lab(migrated_db: str, two_tenants):
    """A tenant with its roster imported — every later stage needs roster import done."""
    tenant_id, _other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        rows, problems = roster.parse_roster_csv(ROSTER_CSV)
        assert problems == []
        result = roster.import_roster(session, tenant_id=tenant_id, rows=rows)

        by_code = {u.employee_code: u.id for u in result.created}
        radiologist_user_id = by_code["R1"]
        profile_id = next(p.id for p in result.profiles_created if p.user_id == radiologist_user_id)
        return {"tenant_id": tenant_id, "radiologist_user_id": radiologist_user_id, "transcriptionist_user_id": by_code["T1"], "radiologist_profile_id": profile_id}


# ============================================= roster & voice enrollment =====
def test_s0_creates_users_and_profiles_and_is_idempotent(migrated_db: str, lab) -> None:
    """Re-running roster import over a longer roster is the `new_radiologist` trigger working as designed, not a duplicate-key error."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        users = session.query(AppUser).filter(AppUser.tenant_id == tenant_id).all()
        assert {u.employee_code for u in users} == {"R1", "R2", "T1"}
        profiles = session.query(RadiologistProfile).filter(RadiologistProfile.tenant_id == tenant_id).all()
        # Two radiologists, one transcriptionist: a profile each for R1 and R2.
        assert len(profiles) == 2

        extended = ROSTER_CSV + b"R3,Dr Chandra,c@lab.in,radiologist\n"
        rows, _ = roster.parse_roster_csv(extended)
        second = roster.import_roster(session, tenant_id=tenant_id, rows=rows)

        assert [u.employee_code for u in second.created] == ["R3"]
        assert len(second.updated) == 3


def test_s0_refuses_a_voiceprint_with_no_consent(migrated_db: str, lab) -> None:
    """A voiceprint is biometric data; an empty ref is no lawful basis."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        with pytest.raises(ConsentRequired):
            roster.enroll_voice(session, tenant_id=tenant_id, radiologist_id=lab["radiologist_profile_id"], embedding=[0.1] * 192, consent_ref="   ")

        profile = roster.enroll_voice(session, tenant_id=tenant_id, radiologist_id=lab["radiologist_profile_id"], embedding=[0.1] * 192, consent_ref="CONSENT-2026-001")
        assert profile.voice_consent_ref == "CONSENT-2026-001"
        assert profile.voice_enrolled_at is not None


def test_s0_keeps_the_two_consents_separate(migrated_db: str, lab) -> None:
    """Enrollment and training are two purposes, so two consents."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        roster.enroll_voice(session, tenant_id=tenant_id, radiologist_id=lab["radiologist_profile_id"], embedding=[0.1] * 192, consent_ref="CONSENT-2026-001")
        profile = roster.record_training_consent(session, tenant_id=tenant_id, radiologist_id=lab["radiologist_profile_id"], consent_ref=None)
        assert profile.voice_consent_ref is not None
        assert profile.training_consent_ref is None


# ======================================================= template import =====
def _submit_and_approve(session, tenant_id, reviewer_id, docs):
    """Template import through its three gates, returning the batch."""
    submission = templates.submit_templates(session, tenant_id=tenant_id, uploads=[ArtifactUpload(filename=name, data=make_docx(body)) for name, body, _spoken in docs])
    for candidate, (_name, _body, spoken) in zip(submission.candidates, docs, strict=False):
        templates.review_candidate(session, tenant_id=tenant_id, candidate_id=candidate.id, reviewer_id=reviewer_id, spoken_study_code=spoken, decision=CandidateReviewStatus.APPROVED)
    return submission.batch


def test_s1_promotes_only_approved_candidates(migrated_db: str, lab) -> None:
    """Only radiologist-approved candidates are promoted, as a refusal rather than a convention."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        submission = templates.submit_templates(session, tenant_id=tenant_id, uploads=[ArtifactUpload(filename="ct_chest.docx", data=make_docx(CHEST_CT_DOC))])
        assert len(submission.candidates) == 1
        assert submission.batch.status == ImportStatus.AWAITING_REVIEW

        # Nothing approved yet.
        with pytest.raises(ApprovalRequired):
            templates.apply_templates(session, tenant_id=tenant_id, batch=submission.batch, approver_id=reviewer)

        templates.review_candidate(session, tenant_id=tenant_id, candidate_id=submission.candidates[0].id, reviewer_id=reviewer, spoken_study_code="ct chest plain", decision=CandidateReviewStatus.APPROVED)
        result = templates.apply_templates(session, tenant_id=tenant_id, batch=submission.batch, approver_id=reviewer)

        assert len(result.versions_created) == 1
        version = result.versions_created[0]
        assert version.is_current is True
        assert version.version == 1
        assert version.approved_by == reviewer
        assert version.spoken_study_code == "ct chest plain"
        assert version.spoken_study_code_phonetic
        assert submission.batch.status == ImportStatus.APPLIED


def test_s1_writes_every_field_as_leave_blank_flag(migrated_db: str, lab) -> None:
    """by construction: V1 auto-fills nothing."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        batch = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "ct chest plain")])
        templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=reviewer)

        fields = session.query(TemplateField).filter(TemplateField.tenant_id == tenant_id).all()
        assert len(fields) == 4
        assert {f.absence_policy for f in fields} == {AbsencePolicy.LEAVE_BLANK_FLAG}
        assert all(f.default_normal_text is None for f in fields)
        assert all(not f.is_required for f in fields)
        # Sections survive the round trip — extraction runs per section.
        assert {f.section for f in fields} == {"FINDINGS", "IMPRESSION"}


def test_s1_refuses_a_batch_that_would_introduce_a_blocking_collision(migrated_db: str, lab) -> None:
    """'s gate, at the point it has to hold."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        batch = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "LMC"), ("usg_abdo.docx", USG_ABDO_DOC, "LMP")])

        with pytest.raises(BatchBlocked):
            templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=reviewer)

        findings = session.query(CollisionAuditFinding).filter(CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.severity == CollisionSeverity.BLOCK).all()
        assert findings, "LMC vs LMP must raise a block-severity finding"
        assert {findings[0].label_a, findings[0].label_b} == {"LMC", "LMP"}

        # Nothing was written: the refusal is whole-batch.
        assert session.query(TemplateVersion).filter(TemplateVersion.tenant_id == tenant_id).count() == 0

        # Resolving the finding unblocks the apply.
        lexicon.resolve_finding(session, tenant_id=tenant_id, finding_id=findings[0].id, resolution=CollisionResolution.RENAMED, resolved_by=reviewer)
        result = templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=reviewer)
        assert len(result.versions_created) == 2


def test_s1_revert_repoints_is_current_without_deleting(migrated_db: str, lab) -> None:
    """The rollback is a pointer move — which is why "new version, never mutate" was worth the cost."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        first = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "ct chest")])
        templates.apply_templates(session, tenant_id=tenant_id, batch=first, approver_id=reviewer)

        revised = list(CHEST_CT_DOC) + [("Hila: Normal", False)]
        second = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest_v2.docx", revised, "ct chest")])
        # Same template code, so this becomes version 2 of the same template.
        templates.apply_templates(session, tenant_id=tenant_id, batch=second, approver_id=reviewer)

        versions = session.query(TemplateVersion).filter(TemplateVersion.tenant_id == tenant_id).order_by(TemplateVersion.version).all()
        assert [v.version for v in versions] == [1, 2]
        assert [v.is_current for v in versions] == [False, True]

        templates.revert_applied_templates(session, tenant_id=tenant_id, batch=second, actor_id=reviewer)
        session.refresh(versions[0])
        session.refresh(versions[1])
        assert versions[0].is_current is True
        assert versions[1].is_current is False
        # Nothing deleted: reports filed against v2 must still resolve.
        assert session.query(TemplateVersion).filter(TemplateVersion.tenant_id == tenant_id).count() == 2


# ========================================================= report corpus =====
CORPUS_TEXTS = [("CT Chest Plain\nLungs: Normal in size and attenuation\nPleura: No effusion\nMediastinum: No significant adenopathy\nSummary: Unremarkable study"), ("CT Chest Plain\nLungs: Normal in size and attenuation\nPleura: No effusion\nMediastinum: Largest node 1.1 cm\nSummary: Normal study"), ("USG Abdomen\nLiver: Normal in size and echotexture\nGallbladder: Unremarkable, no calculi\nKidneys: Both kidneys are normal in size")]


def test_s2_derives_the_map_and_gates_the_histogram_on_verification(migrated_db: str, lab) -> None:
    """The map is derived.: it is not trusted until hand-verified."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        batch = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "ct chest plain"), ("usg_abdo.docx", USG_ABDO_DOC, "usg abdomen")])
        templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=reviewer)

        load = corpus.load_corpus(session, tenant_id=tenant_id, records=[corpus.CorpusRecord(report_text=text, external_report_id=f"RPT-{i}", referring_doctor="Dr Rao" if i < 2 else "Dr Iyer") for i, text in enumerate(CORPUS_TEXTS)])
        assert load.loaded == 3

        # Re-loading the same export is a no-op, not a second corpus.
        again = corpus.load_corpus(session, tenant_id=tenant_id, records=[corpus.CorpusRecord(report_text=CORPUS_TEXTS[0], external_report_id="RPT-0")])
        assert (again.loaded, again.duplicates) == (0, 1)

        mapping = corpus.derive_template_map(session, tenant_id=tenant_id)
        assert mapping.mapped == 3
        assert mapping.by_method[MatchMethod.EXPLICIT] == 3

        histogram = corpus.usage_histogram(session, tenant_id=tenant_id)
        assert histogram[0].count == 2
        assert histogram[0].cumulative_share == pytest.approx(0.6667, abs=1e-3)

        verified, target = corpus.verification_progress(session, tenant_id)
        assert (verified, target) == (0, corpus.VERIFIED_MAPPING_TARGET)

        row = session.query(CorpusReportTemplateMap).filter(CorpusReportTemplateMap.tenant_id == tenant_id).first()
        corpus.verify_mapping(session, tenant_id=tenant_id, mapping_id=row.id, verified_by=reviewer)
        verified, _ = corpus.verification_progress(session, tenant_id)
        assert verified == 1

        corpus.refresh_usage_counts(session, tenant_id=tenant_id)
        chest = session.query(Template).filter(Template.tenant_id == tenant_id, Template.code == "CT_CHEST").one()
        assert chest.usage_count_12m == 2

        prior = corpus.referrer_prior(session, tenant_id=tenant_id, min_reports=2)
        assert prior["Dr Rao"]["CT_CHEST"] == 1.0


# =========================================================== term mining =====
def test_s3_is_rerunnable_and_increments_the_pass_number(migrated_db: str, lab) -> None:
    """Plan: the term mining↔verbatim annotation loop is designed, so term mining must be re-runnable from the start. Pass 2 refreshes ranking and leaves curation alone."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        corpus.load_corpus(session, tenant_id=tenant_id, records=[corpus.CorpusRecord(report_text="LMP noted. USG performed.", external_report_id=f"R{i}") for i in range(8)])

        first = lexicon.run_mining(session, tenant_id=tenant_id, min_frequency=5)
        assert first.run.pass_number == 1
        assert first.terms_new > 0
        # Every new term is pending review: the term mining stage's gate is polysemy review, and
        # auto-accepting on frequency is what puts PA in with one meaning.
        assert first.run.terms_auto_accepted == 0
        assert first.terms_pending_review == first.terms_new

        second = lexicon.run_mining(session, tenant_id=tenant_id, min_frequency=5)
        assert second.run.pass_number == 2
        assert second.terms_new == 0


# =================================================== verbatim annotation =====
@pytest.fixture
def recording(migrated_db: str, lab):
    """One recording on the `current` capture class, ready for annotation."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        patient = Patient(tenant_id=tenant_id, **synth_patient_fields(seed=7))
        session.add(patient)
        session.flush()
        study = Study(tenant_id=tenant_id, patient_id=patient.id)
        session.add(study)
        session.flush()
        rec = Recording(tenant_id=tenant_id, study_id=study.id, radiologist_id=lab["radiologist_profile_id"], object_key=f"rec/{uuid.uuid4().hex}.flac", content_hash=uuid.uuid4().hex, duration_seconds=600, capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT, audio_format=AudioFormat.FLAC)
        session.add(rec)
        session.flush()
        return rec.id


def test_s4_records_verbatim_and_copies_the_device_class(migrated_db: str, lab, recording) -> None:
    """The split is the entire mitigation for, so `capture_device_class` comes from the recording rather than the caller."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        transcript = paired_audio.submit_verbatim(session, tenant_id=tenant_id, recording_id=recording, text="CT chest um plain, the lungs are, are normal in attenuation.", annotator_id=lab["transcriptionist_user_id"], includes_disfluencies=True)
        assert transcript.capture_device_class == CaptureDeviceClass.DICTATION_MIC_PTT
        assert float(transcript.audio_duration_seconds) == 600.0

        progress = paired_audio.gold_partition_progress(session, tenant_id=tenant_id)
        assert progress["current"] == (1, paired_audio.CURRENT_GOLD_TARGET)
        assert progress["legacy"] == (0, paired_audio.LEGACY_GOLD_TARGET)

        hours = paired_audio.corpus_hours(session, tenant_id=tenant_id)
        assert hours.current_hours == pytest.approx(600 / 3600, abs=1e-3)
        assert hours.training_eligible_hours > 0
        assert hours.meets_global_adapter_floor is False


def test_s4_cleaned_transcripts_are_recorded_but_never_training_eligible(migrated_db: str, lab, recording) -> None:
    """A cleaned-up transcript teaches an ASR model to delete words."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        paired_audio.submit_verbatim(session, tenant_id=tenant_id, recording_id=recording, text="CT chest plain. The lungs are normal in attenuation.", annotator_id=lab["transcriptionist_user_id"], includes_disfluencies=False, source=VerbatimSource.IMPORTED)
        hours = paired_audio.corpus_hours(session, tenant_id=tenant_id)
        assert hours.current_hours > 0
        assert hours.training_eligible_hours == 0.0


def test_s4_mines_variants_back_into_s3(migrated_db: str, lab, recording) -> None:
    """The half of the loop term mining cannot do alone: only a real transcript shows how a term actually comes back from ASR."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        lexicon_set = lexicon.get_or_create_tenant_lexicon(session, tenant_id)
        from radreport.core.types import TermType
        from radreport.db.models.knowledge import LexiconTerm
        from radreport.knowledge.phonetics import double_metaphone

        primary, secondary = double_metaphone("echotexture")
        term = LexiconTerm(tenant_id=tenant_id, lexicon_set_id=lexicon_set.id, canonical_form="echotexture", term_type=TermType.ANATOMY, phonetic_key_primary=primary, phonetic_key_secondary=secondary)
        session.add(term)
        session.flush()

        # What ASR actually returns: the same word, split.
        paired_audio.submit_verbatim(session, tenant_id=tenant_id, recording_id=recording, text=("Liver is normal in echo texture. The echo texture is homogeneous throughout."), annotator_id=lab["transcriptionist_user_id"], includes_disfluencies=True)
        result = paired_audio.mine_surface_variants(session, tenant_id=tenant_id, min_occurrences=2)
        assert result.transcripts_scanned == 1
        assert result.variants_written >= 1

        from radreport.db.models.knowledge import LexiconSurfaceVariant

        variants = session.query(LexiconSurfaceVariant).filter(LexiconSurfaceVariant.lexicon_term_id == term.id).all()
        assert "echo texture" in {v.surface_text for v in variants}
        split = next(v for v in variants if v.surface_text == "echo texture")
        assert split.review_status == "auto_approved" and float(split.confidence) > 0.85 and split.threshold_arm == "A"
        assert result.auto_approved >= 1
        from radreport.db.models.orchestration import AuditLog

        assert session.query(AuditLog).filter(AuditLog.action == "lexicon_variant_auto_approved", AuditLog.entity_id == term.id).count() >= 1


# =========================================================== boilerplate =====
def test_s5_ranks_normals_and_never_auto_fills_a_critical_field(migrated_db: str, lab) -> None:
    """Pass 2, with the boundary enforced."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        batch = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "ct chest plain")])
        templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=reviewer)
        corpus.load_corpus(session, tenant_id=tenant_id, records=[corpus.CorpusRecord(report_text=CORPUS_TEXTS[0], external_report_id="C0"), corpus.CorpusRecord(report_text=CORPUS_TEXTS[1], external_report_id="C1")])
        corpus.derive_template_map(session, tenant_id=tenant_id)

        result = boilerplate.mine_boilerplate(session, tenant_id=tenant_id)
        assert result.candidates_written > 0

        ranked = boilerplate.rank_candidates(session, tenant_id=tenant_id)
        lungs = next(r for r in ranked if r.field_key == "lungs")
        assert lungs.candidate_text == "Normal in size and attenuation"
        assert lungs.corpus_share == pytest.approx(1.0)

        csv_text = boilerplate.export_candidates_csv(session, tenant_id=tenant_id)
        assert "decision (approve/reject)" in csv_text
        assert "Normal in size and attenuation" in csv_text

        # Promotion without auto-fill: the text is stored, the policy is not.
        field = boilerplate.promote_candidate(session, tenant_id=tenant_id, candidate_id=lungs.candidate_id, approved_by=reviewer)
        assert field.default_normal_text == "Normal in size and attenuation"
        assert field.absence_policy == AbsencePolicy.LEAVE_BLANK_FLAG

        # Mark it critical, then try to auto-fill it.
        field.is_critical = True
        session.flush()
        with pytest.raises(ValueError, match="critical"):
            boilerplate.promote_candidate(session, tenant_id=tenant_id, candidate_id=lungs.candidate_id, approved_by=reviewer, enable_auto_fill=True)


# =============================================== critical-findings rules =====
def test_s6_seeds_inactive_rules_and_requires_an_escalation_path(migrated_db: str, lab) -> None:
    """The alert path must exist before the queue does. A rule with an SLA and nobody to call is a log line, not an alert."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        seeded = critical_rules.seed_candidate_rules(session, tenant_id=tenant_id)
        assert len(seeded.created) == len(critical_rules.BASELINE_FINDINGS)
        assert all(not r.is_active and r.approved_by is None for r in seeded.created)
        assert critical_rules.active_rules(session, tenant_id=tenant_id) == []

        pneumo = next(r for r in seeded.created if r.code == "PNEUMOTHORAX")
        with pytest.raises(ValueError, match="escalation path"):
            critical_rules.approve_rule(session, tenant_id=tenant_id, rule_id=pneumo.id, approved_by=reviewer)

        authored = critical_rules.author_rule(session, tenant_id=tenant_id, code="PNEUMOTHORAX", finding_label="Pneumothorax", pattern="pneumothorax|collapsed lung", severity="red", sla_minutes=30, escalation_path=[{"step": 1, "contact": "reporting radiologist", "after_minutes": 0}, {"step": 2, "contact": "on-call consultant", "after_minutes": 15}], authored_by=reviewer)
        approved = critical_rules.approve_rule(session, tenant_id=tenant_id, rule_id=authored.id, approved_by=reviewer)
        assert approved.is_active is True
        assert len(critical_rules.active_rules(session, tenant_id=tenant_id)) == 1


def test_s6_editing_an_approved_rule_drops_it_back_to_unapproved(migrated_db: str, lab) -> None:
    """A pattern edited after sign-off is how a rule quietly stops matching."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    path = [{"step": 1, "contact": "on-call", "after_minutes": 0}]
    with tenant_session(tenant_id, url=migrated_db) as session:
        rule = critical_rules.author_rule(session, tenant_id=tenant_id, code="FREE_AIR", finding_label="Free air", pattern="free air", severity="red", sla_minutes=30, escalation_path=path, authored_by=reviewer)
        critical_rules.approve_rule(session, tenant_id=tenant_id, rule_id=rule.id, approved_by=reviewer)
        assert rule.is_active is True

        critical_rules.author_rule(session, tenant_id=tenant_id, code="FREE_AIR", finding_label="Free air", pattern="free air|pneumoperitoneum", severity="red", sla_minutes=30, escalation_path=path, authored_by=reviewer)
        assert rule.is_active is False
        assert rule.approved_by is None


# ======================================================== readiness gate =====
def test_s7_stays_red_until_the_blocking_checks_are_satisfied(migrated_db: str, lab) -> None:
    """The gate between onboarding and pilot."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        before = evaluate_readiness(session, tenant_id, persist=False)
        assert before.passed is False
        failing = {o.check_id for o in before.failures}
        assert "collision_audit_clear" not in failing  # nothing mined yet
        assert {"corpus_template_coverage", "voice_enrollment_complete", "gold_set_frozen", "critical_rules_approved", "baseline_cse_measured"} <= failing

        critical_rules.author_rule(session, tenant_id=tenant_id, code="PNEUMOTHORAX", finding_label="Pneumothorax", pattern="pneumothorax", severity="red", sla_minutes=30, escalation_path=[{"step": 1, "contact": "on-call", "after_minutes": 0}], authored_by=reviewer)
        rule = critical_rules.active_rules(session, tenant_id=tenant_id)
        assert rule == []  # authored, not yet approved

        from radreport.db.models.reporting import CriticalFindingRule

        authored = session.query(CriticalFindingRule).filter_by(tenant_id=tenant_id, code="PNEUMOTHORAX").one()
        critical_rules.approve_rule(session, tenant_id=tenant_id, rule_id=authored.id, approved_by=reviewer)

        after = evaluate_readiness(session, tenant_id, persist=False)
        assert after.passed is False
        assert "critical_rules_approved" not in {o.check_id for o in after.failures}
        assert len(after.failures) == len(before.failures) - 1


def test_s7_persists_its_outcomes_for_audit(migrated_db: str, lab) -> None:
    """Makes readiness gate a structural precondition, so the evidence is kept."""
    tenant_id = lab["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        from radreport.db.models.onboarding import OnboardingReadinessCheck

        evaluate_readiness(session, tenant_id, persist=True)
        rows = session.query(OnboardingReadinessCheck).filter(OnboardingReadinessCheck.tenant_id == tenant_id).all()
        assert len(rows) == 7
        assert {r.check_id for r in rows} == {"collision_audit_clear", "corpus_template_coverage", "voice_enrollment_complete", "gold_set_frozen", "critical_rules_approved", "baseline_cse_measured", "template_library_ready"}


def test_a_known_unresolved_collision_blocks_every_batch_that_carries_it(migrated_db: str, lab) -> None:
    """The audit records a pair once; a second batch with the same codes must not slip through on that."""
    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    docs = [("ct_chest.docx", CHEST_CT_DOC, "LMC"), ("usg_abdo.docx", USG_ABDO_DOC, "LMP")]
    with tenant_session(tenant_id, url=migrated_db) as session:
        first = _submit_and_approve(session, tenant_id, reviewer, docs)
        with pytest.raises(BatchBlocked):
            templates.apply_templates(session, tenant_id=tenant_id, batch=first, approver_id=reviewer)

        second = _submit_and_approve(session, tenant_id, reviewer, docs)
        with pytest.raises(BatchBlocked):
            templates.apply_templates(session, tenant_id=tenant_id, batch=second, approver_id=reviewer)
        assert session.query(TemplateVersion).filter(TemplateVersion.tenant_id == tenant_id).count() == 0


def test_reverting_a_batch_marks_it_and_happens_once(migrated_db: str, lab) -> None:
    """The revert used to roll templates back without ever recording it on the batch."""
    from radreport.core.errors import BatchStateError

    tenant_id = lab["tenant_id"]
    reviewer = lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        batch = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "ct chest plain")])
        unapplied = _submit_and_approve(session, tenant_id, reviewer, [("usg_abdo.docx", USG_ABDO_DOC, "usg abdomen")])
        templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=reviewer)

        templates.revert_applied_templates(session, tenant_id=tenant_id, batch=batch, actor_id=reviewer)
        assert batch.reverted_at is not None
        assert session.query(TemplateVersion).filter(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True)).count() == 0

        with pytest.raises(BatchStateError):
            templates.revert_applied_templates(session, tenant_id=tenant_id, batch=batch, actor_id=reviewer)
        with pytest.raises(BatchStateError):
            templates.revert_applied_templates(session, tenant_id=tenant_id, batch=unapplied, actor_id=reviewer)


# ============================================== lists a radiologist works from
def test_a_radiologist_can_list_what_needs_checking(migrated_db: str, lab) -> None:
    """Collision findings and corpus mappings are listed over HTTP, so verify and resolve have ids to work on; lab users show their profile id."""
    from fastapi.testclient import TestClient

    from radreport.admin import onboarding_steps
    from radreport.api.app import create_app
    from tests.db.helpers import lab_headers, make_platform_user, signed_in

    tenant_id, reviewer = lab["tenant_id"], lab["radiologist_user_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        blocked = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "LMC"), ("usg_abdo.docx", USG_ABDO_DOC, "LMP")])
        with pytest.raises(BatchBlocked):
            templates.apply_templates(session, tenant_id=tenant_id, batch=blocked, approver_id=reviewer)
        assert onboarding_steps.propose_template_merges(session, tenant_id, blocked.id)["items"] == [], "the merge proposals come back with ids, here none"
    client = TestClient(create_app())
    headers = lab_headers(migrated_db, tenant_id, "radiologist")
    findings = client.get("/onboarding/collision-findings", headers=headers)
    assert findings.status_code == 200 and (frozenset({"LMC", "LMP"}), "block") in {(frozenset({f["label_a"], f["label_b"]}), f["severity"]) for f in findings.json()}
    finding_id = findings.json()[0]["finding_id"]
    assert client.post(f"/onboarding/collision-findings/{finding_id}/resolve", json={"resolution": "renamed"}, headers=headers).status_code == 200
    assert finding_id not in {f["finding_id"] for f in client.get("/onboarding/collision-findings", headers=headers).json()}

    with tenant_session(tenant_id, url=migrated_db) as session:
        batch = _submit_and_approve(session, tenant_id, reviewer, [("ct_chest.docx", CHEST_CT_DOC, "ct chest"), ("usg_abdo.docx", USG_ABDO_DOC, "ultrasound abdomen")])
        templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=reviewer)
        corpus.load_corpus(session, tenant_id=tenant_id, records=[corpus.CorpusRecord(report_text=t, external_report_id=f"C{i}") for i, t in enumerate(CORPUS_TEXTS)])
        corpus.derive_template_map(session, tenant_id=tenant_id)
    mappings = client.get("/onboarding/corpus/mappings", headers=headers)
    assert mappings.status_code == 200 and mappings.headers["x-total-count"] == "3" and {m["template_code"] for m in mappings.json()} == {"CT_CHEST", "US_ABDOMEN"}
    first = mappings.json()[0]["mapping_id"]
    assert client.post(f"/onboarding/corpus/mappings/{first}/verify", json={}, headers=headers).status_code == 200
    assert client.get("/onboarding/corpus/mappings", params={"verified": "true"}, headers=headers).json()[0]["mapping_id"] == first

    admin = signed_in(make_platform_user(migrated_db))
    users = {u["employee_code"]: u for u in admin.get(f"/admin/api/labs/{tenant_id}/users").json()}
    assert users["R1"]["radiologist_profile_id"] == str(lab["radiologist_profile_id"]) and users["T1"]["radiologist_profile_id"] is None
