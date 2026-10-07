"""The review screen's endpoints: fetch a draft, edit it, sign it, and grade it afterwards.

Order: take work from the queue (get_queue, get_queue_stats) -> open a draft and its audio
(get_draft, get_audio) -> save edits (post_revision) -> sign (post_signature) or amend later
(post_addendum) -> acknowledge urgent findings (post_acknowledgement) -> grade and rate the
draft (post_grade, get_cse_rate, post_usefulness, get_usefulness).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from radreport.adapters.storage.object_store import object_store
from radreport.api.deps import CurrentPrincipal, DbSession
from radreport.cache.lookups import user_roles
from radreport.core.config import get_settings
from radreport.core.tenancy import Principal
from radreport.db.models.ingestion import Recording
from radreport.db.models.reporting import ReportDraft
from radreport.review import feedback, grading, signing
from radreport.review import queue as review_queue
from radreport.review import session as review_session
from radreport.review.rbac import PermissionDenied, Reviewer
from radreport.review.signing import SigningRefused

router = APIRouter(prefix="/review", tags=["review"])


def _reviewer(session: DbSession, principal: Principal) -> Reviewer:
    """Build the acting reviewer from stored roles, not from the request."""
    assert principal.tenant_id is not None
    user = user_roles(session, principal.tenant_id, principal.id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unknown or inactive user")
    return Reviewer(user_id=user.user_id, roles=user.roles)


def _tenant(principal: Principal) -> uuid.UUID:
    assert principal.tenant_id is not None
    return principal.tenant_id


def _guard(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionDenied):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, SigningRefused):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


# ==================================================================== queue ==
class QueueItemOut(BaseModel):
    draft_id: uuid.UUID
    template_code: str
    template_display_name: str
    priority: str
    confidence: float
    flagged_field_count: int
    status: str
    has_critical_alert: bool
    requires_radiologist: bool
    waiting_minutes: int


@router.get("/queue", response_model=list[QueueItemOut])
def get_queue(session: DbSession, principal: CurrentPrincipal, limit: int = 50) -> list[QueueItemOut]:
    """Flagged-first within priority, filtered to what this role may complete."""
    reviewer = _reviewer(session, principal)
    try:
        items = review_queue.build_queue(session, tenant_id=_tenant(principal), reviewer=reviewer, limit=limit)
    except PermissionDenied as exc:
        raise _guard(exc) from exc
    return [QueueItemOut(draft_id=i.draft_id, template_code=i.template_code, template_display_name=i.template_display_name, priority=i.priority, confidence=i.confidence, flagged_field_count=i.flagged_field_count, status=i.status, has_critical_alert=i.has_critical_alert, requires_radiologist=i.requires_radiologist, waiting_minutes=i.waiting_minutes) for i in items]


@router.get("/queue/stats")
def get_queue_stats(session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """Counts and waiting times — no clinical content."""
    reviewer = _reviewer(session, principal)
    try:
        stats = review_queue.queue_stats(session, tenant_id=_tenant(principal), reviewer=reviewer)
    except PermissionDenied as exc:
        raise _guard(exc) from exc
    return {"total": stats.total, "awaiting_radiologist": stats.awaiting_radiologist, "awaiting_assistant": stats.awaiting_assistant, "with_critical_alert": stats.with_critical_alert, "oldest_waiting_minutes": stats.oldest_waiting_minutes, "median_flagged_fields": stats.median_flagged_fields}


# ==================================================================== draft ==
@router.get("/drafts/{draft_id}")
def get_draft(draft_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """Open a draft for review. Fields come back flagged-first."""
    reviewer = _reviewer(session, principal)
    tenant_id = _tenant(principal)
    try:
        view = review_session.open_draft(session, tenant_id=tenant_id, draft_id=draft_id, reviewer=reviewer)
    except (PermissionDenied, ValueError) as exc:
        raise _guard(exc) from exc

    checks = signing.preflight(session, tenant_id=tenant_id, draft_id=draft_id)
    return {
        "draft_id": str(view.draft_id),
        "status": view.status,
        "rendered_text": view.rendered_text,
        "overall_confidence": view.overall_confidence,
        "fields": [
            {
                "field_value_id": str(f.field_value_id),
                "field_key": f.field_key,
                "display_label": f.display_label,
                "section": f.section,
                "value_text": f.value_text,
                "value_enum": f.value_enum,
                "value_numeric": f.value_numeric,
                "value_unit": f.value_unit,
                "assertion_status": f.assertion_status,
                "laterality": f.laterality,
                "fill_source": f.fill_source,
                # the most important visual distinction.
                "system_asserted": f.system_asserted,
                "default_normal_text": f.default_normal_text,
                "is_grounded": f.is_grounded,
                "is_flagged": f.is_flagged,
                "is_critical": f.is_critical,
                "confidence": f.confidence,
                "flag_reasons": list(f.flag_reasons),
                "provenance": [dict(p) for p in f.provenance],
            }
            for f in view.fields
        ],
        "findings": view.findings,
        "signing": {"may_sign": checks.may_sign, "blocking_findings": list(checks.blocking_findings), "unacknowledged_alerts": list(checks.unacknowledged_alerts), "ungrounded_fields": list(checks.ungrounded_fields)},
    }


@router.get("/drafts/{draft_id}/audio")
def get_audio(draft_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> Response:
    """The dictation behind a draft, for per-field click-to-listen."""
    reviewer = _reviewer(session, principal)
    if not reviewer.can("view_draft"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "view_draft is required")

    draft = session.get(ReportDraft, draft_id)
    if draft is None or draft.tenant_id != _tenant(principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no report_draft {draft_id}")
    recording = session.get(Recording, draft.recording_id)
    if recording is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no recording for this draft")

    store = object_store()
    media = "audio/flac" if recording.audio_format == "flac" else "audio/wav"
    signer = getattr(store, "signed_url", None)
    if signer is not None:
        # Straight from the bucket on a link that expires in a minute: the audio never passes through the app or any CDN.
        try:
            url = signer(recording.object_key, expires_seconds=get_settings().storage.signed_url_seconds, content_type=media)
        except Exception as exc:  # noqa: BLE001 - the store's failures are opaque
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"audio unavailable: {exc}") from exc
        return Response(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": url, "Cache-Control": "private, no-store"})
    try:
        data = store.get(recording.object_key)
    except Exception as exc:  # noqa: BLE001 - the store's failures are opaque
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"audio unavailable: {exc}") from exc

    return Response(
        data,
        media_type=media,
        # Never cached by an intermediary: this is PHI.
        headers={"Cache-Control": "private, no-store"},
    )


class FieldEditIn(BaseModel):
    field_value_id: uuid.UUID
    value_text: str | None = None
    value_numeric: float | None = None
    value_unit: str | None = None
    value_enum: str | None = None
    assertion_status: str | None = None
    laterality: str | None = None


class RevisionIn(BaseModel):
    edits: list[FieldEditIn] = Field(default_factory=list)
    rendered_text: str
    active_edit_seconds: int = Field(ge=0)
    """**Focus time**, measured by the review screen's blur/focus timer."""

    wall_clock_seconds: int = Field(ge=0)


@router.post("/drafts/{draft_id}/revisions")
def post_revision(draft_id: uuid.UUID, body: RevisionIn, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """Record a reviewer's edits, the revision, and the per-field diffs."""
    reviewer = _reviewer(session, principal)
    try:
        result = review_session.record_revision(session, tenant_id=_tenant(principal), draft_id=draft_id, reviewer=reviewer, edits=[review_session.FieldEdit(**e.model_dump()) for e in body.edits], active_edit_seconds=body.active_edit_seconds, wall_clock_seconds=body.wall_clock_seconds, rendered_text=body.rendered_text)
    except (PermissionDenied, ValueError) as exc:
        raise _guard(exc) from exc

    return {"revision_id": str(result.revision.id), "revision_number": result.revision.revision_number, "active_edit_seconds": result.active_edit_seconds, "clamped": result.clamped, "edits": [{"field_value_id": str(e.report_field_value_id), "edit_type": e.edit_type, "error_category": e.error_category, "has_audio_span": e.audio_start_ms is not None} for e in result.edit_events]}


# ================================================================== signing ==
@router.post("/drafts/{draft_id}/sign")
def post_signature(draft_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """Sign. **Radiologist only**, and refused on any outstanding gate."""
    reviewer = _reviewer(session, principal)
    try:
        final = signing.sign_report(session, tenant_id=_tenant(principal), draft_id=draft_id, reviewer=reviewer)
    except (PermissionDenied, SigningRefused, ValueError) as exc:
        raise _guard(exc) from exc
    return {"final_report_id": str(final.id), "content_hash": final.content_hash, "path_type": final.path_type, "signed_at": final.signed_at.isoformat()}


class AmendIn(BaseModel):
    rendered_text: str
    reason: str = Field(min_length=1)


@router.post("/reports/{report_id}/addendum")
def post_addendum(report_id: uuid.UUID, body: AmendIn, session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """Issue an addendum. The signed original is never modified."""
    reviewer = _reviewer(session, principal)
    try:
        addendum = signing.amend_report(session, tenant_id=_tenant(principal), original_report_id=report_id, reviewer=reviewer, rendered_text=body.rendered_text, reason=body.reason)
    except (PermissionDenied, ValueError) as exc:
        raise _guard(exc) from exc
    return {"addendum_id": str(addendum.id), "amends": str(report_id)}


class AcknowledgeIn(BaseModel):
    outcome: str
    """`true_positive` / `false_positive`."""


@router.post("/alerts/{alert_id}/acknowledge")
def post_acknowledgement(alert_id: uuid.UUID, body: AcknowledgeIn, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    reviewer = _reviewer(session, principal)
    try:
        alert = signing.acknowledge_alert(session, tenant_id=_tenant(principal), alert_id=alert_id, reviewer=reviewer, outcome=body.outcome)
    except (PermissionDenied, ValueError) as exc:
        raise _guard(exc) from exc
    return {"alert_id": str(alert.id), "outcome": alert.outcome, "is_breach": alert.is_breach}


# ================================================================= grading ===
class GradeIn(BaseModel):
    grade: str
    note: str | None = None


@router.post("/reports/{report_id}/grade")
def post_grade(report_id: uuid.UUID, body: GradeIn, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """G0–G4. **Radiologist only** — it is a clinical judgement."""
    reviewer = _reviewer(session, principal)
    try:
        result = grading.grade_report(session, tenant_id=_tenant(principal), final_report_id=report_id, grade=body.grade, reviewer=reviewer, note=body.note)
    except (PermissionDenied, ValueError) as exc:
        raise _guard(exc) from exc
    return {"final_report_id": str(result.final_report_id), "grade": result.grade, "is_cse": result.is_cse, "previous_grade": result.previous_grade}


@router.get("/metrics/cse-rate")
def get_cse_rate(session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """The observed CSE rate, with its denominator."""
    reviewer = _reviewer(session, principal)
    if not reviewer.can("grade_report"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "radiologist only")
    rate = grading.cse_rate(session, tenant_id=_tenant(principal))
    return {"graded": rate.graded, "cse_count": rate.cse_count, "rate": rate.rate, "is_reportable": rate.is_reportable}


# ================================================================ feedback ===
class UsefulnessIn(BaseModel):
    was_useless: bool
    reason: str | None = None


@router.post("/drafts/{draft_id}/usefulness")
def post_usefulness(draft_id: uuid.UUID, body: UsefulnessIn, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """The one-click verdict — and it is actually recorded."""
    reviewer = _reviewer(session, principal)
    try:
        record = feedback.report_usefulness(session, tenant_id=_tenant(principal), draft_id=draft_id, reviewer=reviewer, was_useless=body.was_useless, reason=body.reason)
    except (PermissionDenied, ValueError) as exc:
        raise _guard(exc) from exc
    return {"draft_id": str(draft_id), "was_useless": record.was_useless}


@router.get("/metrics/usefulness")
def get_usefulness(session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """'s disengagement early warning."""
    _reviewer(session, principal)
    stats = feedback.usefulness_stats(session, tenant_id=_tenant(principal))
    return {"reported": stats.reported, "useless": stats.useless, "rate": stats.rate, "is_alarming": stats.is_alarming}
