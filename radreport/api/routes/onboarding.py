"""The lab side of onboarding: the consents and clinical approvals only the lab's own staff can give.

Order: voices (enroll_voice, training_consent) -> templates (list_candidates, review_candidate,
decide_merge, apply_batch, revert_batch) -> historical reports (verify_mapping, histogram,
referrer_prior) -> vocabulary (resolve_finding) -> verbatim transcripts (verbatim_queue,
submit_verbatim) -> boilerplate (export_boilerplate, promote_boilerplate) -> critical-finding
rules (author_rule, approve_rule). Uploads and mining steps are run from the admin panel.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from radreport.api.deps import CurrentPrincipal, DbSession
from radreport.core.errors import ApprovalRequired, BatchBlocked, BatchStateError, ConsentRequired
from radreport.core.tenancy import Principal
from radreport.core.types import CandidateReviewStatus, UserRole
from radreport.db.models.identity import AppUser
from radreport.db.models.onboarding import ImportBatch
from radreport.onboarding import boilerplate, corpus, critical_rules, lexicon, paired_audio, roster, templates

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _tenant_of(principal: Principal, session: DbSession) -> uuid.UUID:
    """The one tenant this request is bound to."""
    assert principal.tenant_id is not None
    return principal.tenant_id


def _require_role(session: DbSession, principal: Principal, role: str) -> uuid.UUID:
    """Assert the caller holds `role` in this tenant, and return their user id."""
    user = session.get(AppUser, principal.id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unknown or inactive user")
    if role not in (user.roles or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"this action requires the {role!r} role")
    return user.id


def _uploader(session: DbSession, principal: Principal) -> uuid.UUID:
    """A lab admin or radiologist acting for the lab; returns their user id."""
    user = session.get(AppUser, principal.id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unknown or inactive user")
    if not {UserRole.LAB_ADMIN, UserRole.RADIOLOGIST} & set(user.roles or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action requires the 'lab_admin' or 'radiologist' role")
    return user.id


def _get_batch(session: DbSession, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no import_batch {batch_id}")
    return batch


# ======================================================= voice enrollment =====
class VoiceEnrollmentRequest(BaseModel):
    embedding: list[float] = Field(min_length=192, max_length=192)
    consent_ref: str = Field(min_length=1)
    """No voiceprint without a signed consent reference."""


@router.post("/radiologists/{radiologist_id}/voice-enrollment")
def enroll_voice(radiologist_id: uuid.UUID, body: VoiceEnrollmentRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """The roster import stage's human gate — enrollment consent, captured with the voiceprint."""
    tenant_id = _tenant_of(principal, session)
    actor = _uploader(session, principal)
    try:
        profile = roster.enroll_voice(session, tenant_id=tenant_id, radiologist_id=radiologist_id, embedding=body.embedding, consent_ref=body.consent_ref, actor_id=actor)
    except ConsentRequired as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"radiologist_id": str(profile.id), "voice_consent_ref": profile.voice_consent_ref or ""}


class TrainingConsentRequest(BaseModel):
    consent_ref: str | None = None
    """The second consent. `null` records a refusal, which is a real answer — the radiologist stays enrolled and their audio never pools."""


@router.post("/radiologists/{radiologist_id}/training-consent")
def training_consent(radiologist_id: uuid.UUID, body: TrainingConsentRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, bool]:
    tenant_id = _tenant_of(principal, session)
    actor = _uploader(session, principal)
    try:
        profile = roster.record_training_consent(session, tenant_id=tenant_id, radiologist_id=radiologist_id, consent_ref=body.consent_ref, actor_id=actor)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"granted": profile.training_consent_ref is not None}


# ======================================================= template import =====
class CandidateSummary(BaseModel):
    id: uuid.UUID
    proposed_code: str | None
    proposed_spoken_study_code: str | None
    proposed_modality: str | None
    proposed_body_region: str | None
    parse_confidence: float | None
    field_count: int
    needs_field_by_field_review: bool
    merged_into_template_id: uuid.UUID | None


@router.get("/templates/candidates", response_model=list[CandidateSummary])
def list_candidates(session: DbSession, principal: CurrentPrincipal, batch_id: uuid.UUID | None = None) -> list[CandidateSummary]:
    """The review queue, lowest parse confidence first."""
    tenant_id = _tenant_of(principal, session)
    rows = templates.list_pending_review(session, tenant_id=tenant_id, batch_id=batch_id)
    return [CandidateSummary(id=c.id, proposed_code=c.proposed_code, proposed_spoken_study_code=c.proposed_spoken_study_code, proposed_modality=c.proposed_modality, proposed_body_region=c.proposed_body_region, parse_confidence=float(c.parse_confidence) if c.parse_confidence is not None else None, field_count=len((c.proposed_json_schema or {}).get("properties", {})), needs_field_by_field_review=(c.parse_confidence is None or float(c.parse_confidence) < templates.LOW_CONFIDENCE_THRESHOLD), merged_into_template_id=c.merged_into_template_id) for c in rows]


class CandidateReviewRequest(BaseModel):
    decision: str = CandidateReviewStatus.APPROVED
    spoken_study_code: str | None = None
    code: str | None = None
    modality: str | None = None
    body_region: str | None = None
    json_schema: dict[str, Any] | None = None


@router.post("/templates/candidates/{candidate_id}/review")
def review_candidate(candidate_id: uuid.UUID, body: CandidateReviewRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """The per-schema and per-code gate. **Radiologist only**."""
    tenant_id = _tenant_of(principal, session)
    reviewer = _require_role(session, principal, UserRole.RADIOLOGIST)
    try:
        candidate = templates.review_candidate(session, tenant_id=tenant_id, candidate_id=candidate_id, reviewer_id=reviewer, **body.model_dump(exclude={"decision"}), decision=body.decision)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"candidate_id": str(candidate.id), "review_status": candidate.review_status}


class MergeDecisionRequest(BaseModel):
    decision: str


@router.post("/merge-proposals/{proposal_id}/decide")
def decide_merge(proposal_id: uuid.UUID, body: MergeDecisionRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """The per-merge gate. **Radiologist only.**"""
    tenant_id = _tenant_of(principal, session)
    reviewer = _require_role(session, principal, UserRole.RADIOLOGIST)
    try:
        proposal = templates.decide_merge(session, tenant_id=tenant_id, proposal_id=proposal_id, decision=body.decision, decided_by=reviewer)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"proposal_id": str(proposal.id), "decision": proposal.decision}


@router.post("/batches/{batch_id}/apply")
def apply_batch(batch_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """Promote approved candidates into `template_version`."""
    tenant_id = _tenant_of(principal, session)
    approver = _require_role(session, principal, UserRole.RADIOLOGIST)
    batch = _get_batch(session, tenant_id, batch_id)
    try:
        result = templates.apply_templates(session, tenant_id=tenant_id, batch=batch, approver_id=approver)
    except BatchBlocked as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ApprovalRequired as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc)) from exc
    except BatchStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return {"batch_id": str(batch.id), "templates_created": len(result.templates_created), "versions_created": len(result.versions_created), "skipped": [{"candidate_id": str(cid), "reason": r} for cid, r in result.skipped]}


@router.post("/batches/{batch_id}/revert")
def revert_batch(batch_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict[str, int]:
    """The rollback: re-point `is_current` at the previous version."""
    tenant_id = _tenant_of(principal, session)
    actor = _require_role(session, principal, UserRole.RADIOLOGIST)
    batch = _get_batch(session, tenant_id, batch_id)
    try:
        restored = templates.revert_applied_templates(session, tenant_id=tenant_id, batch=batch, actor_id=actor)
    except BatchStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"restored_versions": len(restored)}


# ========================================================= report corpus =====
class VerifyMappingRequest(BaseModel):
    correct_template_id: uuid.UUID | None = None


@router.post("/corpus/mappings/{mapping_id}/verify")
def verify_mapping(mapping_id: uuid.UUID, body: VerifyMappingRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """The corpus load stage's human gate: confirm or correct one derived mapping."""
    tenant_id = _tenant_of(principal, session)
    reviewer = _require_role(session, principal, UserRole.RADIOLOGIST)
    try:
        mapping = corpus.verify_mapping(session, tenant_id=tenant_id, mapping_id=mapping_id, verified_by=reviewer, correct_template_id=body.correct_template_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    verified, target = corpus.verification_progress(session, tenant_id)
    return {"mapping_id": str(mapping.id), "match_method": mapping.match_method, "verified": verified, "verification_target": target}


@router.get("/corpus/histogram")
def histogram(session: DbSession, principal: CurrentPrincipal, verified_only: bool = False) -> list[dict[str, Any]]:
    """Power-law head detection — which ~20 templates V1 ships."""
    tenant_id = _tenant_of(principal, session)
    return [{"template_id": str(e.template_id), "code": e.code, "count": e.count, "share": e.share, "cumulative_share": e.cumulative_share} for e in corpus.usage_histogram(session, tenant_id=tenant_id, verified_only=verified_only)]


@router.get("/corpus/referrer-prior")
def referrer_prior(session: DbSession, principal: CurrentPrincipal) -> dict[str, dict[str, float]]:
    """P(template | referring doctor) for the routing prior."""
    tenant_id = _tenant_of(principal, session)
    return corpus.referrer_prior(session, tenant_id=tenant_id)


# =========================================================== term mining =====
class ResolveFindingRequest(BaseModel):
    resolution: str


@router.post("/collision-findings/{finding_id}/resolve")
def resolve_finding(finding_id: uuid.UUID, body: ResolveFindingRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """The term mining stage's blocking human gate. **Radiologist only** — it is a clinical call."""
    tenant_id = _tenant_of(principal, session)
    reviewer = _require_role(session, principal, UserRole.RADIOLOGIST)
    try:
        finding = lexicon.resolve_finding(session, tenant_id=tenant_id, finding_id=finding_id, resolution=body.resolution, resolved_by=reviewer)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"finding_id": str(finding.id), "resolution": finding.resolution}


# =================================================== verbatim annotation =====
@router.get("/verbatim/queue")
def verbatim_queue(session: DbSession, principal: CurrentPrincipal, limit: int = 50) -> dict[str, Any]:
    """The critical path, as a work queue — `current` before `legacy`."""
    tenant_id = _tenant_of(principal, session)
    queue = paired_audio.build_verbatim_queue(session, tenant_id=tenant_id, limit=limit)
    progress = paired_audio.gold_partition_progress(session, tenant_id=tenant_id)
    hours = paired_audio.corpus_hours(session, tenant_id=tenant_id)
    return {"outstanding": queue.total_outstanding, "current": [{"recording_id": str(i.recording_id), "duration_seconds": i.duration_seconds, "has_verbatim": i.has_verbatim} for i in queue.current], "legacy": [{"recording_id": str(i.recording_id), "duration_seconds": i.duration_seconds, "has_verbatim": i.has_verbatim} for i in queue.legacy], "gold_progress": {k: {"annotated": v[0], "target": v[1]} for k, v in progress.items()}, "corpus_hours": {"current": hours.current_hours, "legacy": hours.legacy_hours, "training_eligible": hours.training_eligible_hours, "meets_global_adapter_floor": hours.meets_global_adapter_floor, "speakers_meeting_floor": hours.speakers_meeting_floor()}}


class VerbatimRequest(BaseModel):
    recording_id: uuid.UUID
    text: str
    includes_disfluencies: bool
    """False ⇒ recorded, but never ASR-training eligible: a cleaned transcript teaches a model to delete words."""

    is_eval_set_member: bool = False


@router.post("/verbatim")
def submit_verbatim(body: VerbatimRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """The verbatim annotation stage's human gate — a transcriptionist produces verbatim ground truth."""
    tenant_id = _tenant_of(principal, session)
    annotator = _require_role(session, principal, UserRole.TRANSCRIPTIONIST)
    try:
        transcript = paired_audio.submit_verbatim(session, tenant_id=tenant_id, recording_id=body.recording_id, text=body.text, annotator_id=annotator, includes_disfluencies=body.includes_disfluencies, is_eval_set_member=body.is_eval_set_member)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"transcript_id": str(transcript.id), "capture_device_class": transcript.capture_device_class, "training_eligible": transcript.includes_disfluencies and not transcript.is_eval_set_member}


# =========================================================== boilerplate =====
@router.get("/boilerplate/export")
def export_boilerplate(session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """The boilerplate ranking stage's V1 deliverable: a CSV, not a screen."""
    tenant_id = _tenant_of(principal, session)
    return {"csv": boilerplate.export_candidates_csv(session, tenant_id=tenant_id)}


class PromoteBoilerplateRequest(BaseModel):
    enable_auto_fill: bool = False
    """The second, separate decision."""


@router.post("/boilerplate/{candidate_id}/promote")
def promote_boilerplate(candidate_id: uuid.UUID, body: PromoteBoilerplateRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """**Radiologist only.** Critical fields refuse auto-fill outright."""
    tenant_id = _tenant_of(principal, session)
    approver = _require_role(session, principal, UserRole.RADIOLOGIST)
    try:
        template_field = boilerplate.promote_candidate(session, tenant_id=tenant_id, candidate_id=candidate_id, approved_by=approver, enable_auto_fill=body.enable_auto_fill)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"template_field_id": str(template_field.id), "absence_policy": template_field.absence_policy, "default_normal_text": template_field.default_normal_text}


# =============================================== critical-findings rules =====
class AuthorRuleRequest(BaseModel):
    code: str
    finding_label: str
    pattern: str
    severity: str
    sla_minutes: int = Field(gt=0)
    escalation_path: list[dict[str, Any]] = Field(min_length=1)
    pattern_type: str = "lexical"
    negation_sensitive: bool = True
    requires_ack: bool = True


@router.post("/critical-rules")
def author_rule(body: AuthorRuleRequest, session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """**Radiologist only.** The label, SLA and escalation path are clinical."""
    tenant_id = _tenant_of(principal, session)
    author = _require_role(session, principal, UserRole.RADIOLOGIST)
    try:
        rule = critical_rules.author_rule(session, tenant_id=tenant_id, authored_by=author, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"rule_id": str(rule.id), "code": rule.code}


@router.post("/critical-rules/{rule_id}/approve")
def approve_rule(rule_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """The critical-findings rules stage's gate. A rule with no escalation path is refused."""
    tenant_id = _tenant_of(principal, session)
    approver = _require_role(session, principal, UserRole.RADIOLOGIST)
    try:
        rule = critical_rules.approve_rule(session, tenant_id=tenant_id, rule_id=rule_id, approved_by=approver)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"rule_id": str(rule.id), "code": rule.code, "is_active": rule.is_active}
