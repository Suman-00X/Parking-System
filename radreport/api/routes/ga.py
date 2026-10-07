"""The lab side of the later-stage features: unreviewed release, hospital-system export and drift monitoring.

Order: autonomy (get_accrual, post_revoke, get_autonomy_coverage) -> export a signed report
(get_hl7, get_fhir) -> monitoring (get_drift). Opening accrual, granting autonomy and the
adaptation gates are product-admin actions in the admin panel's API.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from radreport.api.deps import CurrentPrincipal, DbSession, ReadDbSession
from radreport.autonomy import accrual, grant, release
from radreport.cache.lookups import user_roles
from radreport.core.tenancy import Principal
from radreport.core.types import UserRole
from radreport.db.models.identity import AppUser, Patient, Study
from radreport.db.models.review import FinalReport
from radreport.db.session import tenant_session
from radreport.export.fhir import FhirContext, build_diagnostic_report
from radreport.export.hl7 import ExportRefused, OruContext, build_oru
from radreport.monitoring import drift

router = APIRouter(prefix="/ga", tags=["ga"])


def _tenant(principal: Principal) -> uuid.UUID:
    assert principal.tenant_id is not None
    return principal.tenant_id


def _require_lab_role(session: DbSession, principal: Principal, *roles: str) -> uuid.UUID:
    assert principal.tenant_id is not None
    user = user_roles(session, principal.tenant_id, principal.id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unknown or inactive user")
    if roles and not set(roles) & set(user.roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires one of: {', '.join(roles)}")
    return user.user_id


# ================================================================ autonomy ===
@router.get("/autonomy/{class_code}")
def get_accrual(class_code: str, session: ReadDbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """Current evidence for a class. Read-only; grants nothing."""
    try:
        snapshot = accrual.snapshot(session, tenant_id=_tenant(principal), class_code=class_code)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {
        "class_code": snapshot.class_code,
        "baseline_cse_rate": snapshot.baseline_cse_rate,
        "graded_n": snapshot.graded_n,
        "required_n": snapshot.required_n,
        "observed_cse": snapshot.observed_cse,
        "observed_rate": snapshot.observed_rate,
        "posterior_prob_ni": snapshot.posterior_prob_ni,
        "meets_volume": snapshot.meets_volume,
        # Named in the conditional on purpose: observing and deciding are separate
        # steps, and this endpoint is only the observation.
        "would_support_grant": snapshot.would_support_grant,
    }


class RevokeIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/autonomy/{class_code}/revoke")
def post_revoke(class_code: str, body: RevokeIn, session: DbSession, principal: CurrentPrincipal) -> dict[str, str]:
    """Withdraw autonomy; a product admin can also do this from the admin panel."""
    actor = _require_lab_role(session, principal, UserRole.RADIOLOGIST, UserRole.LAB_ADMIN)
    try:
        klass = grant.revoke(session, tenant_id=_tenant(principal), class_code=class_code, reason=body.reason, actor_id=actor)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"class_code": klass.code, "status": klass.status, "reason": body.reason}


@router.get("/autonomy-coverage")
def get_autonomy_coverage(session: ReadDbSession, principal: CurrentPrincipal, days: int = 30) -> dict[str, Any]:
    """How much review autonomy is actually removing."""
    _require_lab_role(session, principal, UserRole.RADIOLOGIST, UserRole.LAB_ADMIN, UserRole.AUDITOR)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    measured = release.coverage(session, tenant_id=_tenant(principal), since=since)
    return {"window_days": days, "signed": measured.signed, "reviewed": measured.reviewed, "released_without_review": measured.released, "share": measured.share, "target": release.REVIEW_REDUCTION_TARGET, "meets_target": measured.meets_target}


# ================================================================== export ===
def _load_export(session: Session, tenant_id: uuid.UUID, report_id: uuid.UUID) -> tuple[Any, Any, Any, Any] | None:
    final = session.get(FinalReport, report_id)
    if final is None or final.tenant_id != tenant_id:
        return None
    study = session.get(Study, final.study_id)
    patient = session.get(Patient, study.patient_id) if study else None
    signer = session.get(AppUser, final.signed_by)
    return final, study, patient, signer


def _export_context(session: Session, tenant_id: uuid.UUID, report_id: uuid.UUID) -> tuple[Any, Any, Any, Any]:
    """The report and what its message needs, from the replica, or the primary when the replica has not caught up."""
    found = _load_export(session, tenant_id, report_id)
    if found is None and session.info.get("read_only"):
        # Signed moments ago: the replica may not have it yet, and the primary does.
        with tenant_session(tenant_id) as primary:
            found = _load_export(primary, tenant_id, report_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no final_report {report_id}")
    return found


@router.get("/export/{report_id}/hl7")
def get_hl7(report_id: uuid.UUID, session: ReadDbSession, principal: CurrentPrincipal) -> Response:
    """ORU^R01 for a signed report. Refused without an accession."""
    tenant_id = _tenant(principal)
    final, study, patient, signer = _export_context(session, tenant_id, report_id)
    try:
        message = build_oru(context=OruContext(accession_number=study.accession_number or "" if study else "", patient_id=patient.pseudonym if patient else "", patient_family_name=patient.pseudonym if patient else "", patient_sex=(patient.sex if patient and patient.sex else "U"), study_description=study.study_description or "" if study else "", modality=study.modality or "" if study else "", study_datetime=study.study_datetime if study else None, signed_by_name=signer.display_name if signer else "", signed_at=final.signed_at, is_correction=final.amends_report_id is not None), report_text=final.rendered_text, content_hash=final.content_hash, report_id=final.id)
    except ExportRefused as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc)) from exc
    return Response(message.render(), media_type="application/hl7-v2")


@router.get("/export/{report_id}/fhir")
def get_fhir(report_id: uuid.UUID, session: ReadDbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """FHIR R4 DiagnosticReport for a signed report."""
    tenant_id = _tenant(principal)
    final, study, patient, signer = _export_context(session, tenant_id, report_id)
    try:
        return build_diagnostic_report(
            context=FhirContext(
                accession_number=study.accession_number or "" if study else "",
                # A reference, never inline demographics: keeps patient
                # identity out of anything this system originates.
                patient_reference=f"Patient/{patient.pseudonym}" if patient else "Patient/unknown",
                study_description=study.study_description or "" if study else "",
                modality=study.modality or "" if study else "",
                study_datetime=study.study_datetime if study else None,
                performer_display=signer.display_name if signer else "",
                signed_at=final.signed_at,
                is_amendment=final.amends_report_id is not None,
                amends_identifier=str(final.amends_report_id) if final.amends_report_id else None,
            ),
            report_text=final.rendered_text,
            content_hash=final.content_hash,
            report_id=final.id,
            structured_payload=final.structured_payload,
        )
    except ExportRefused as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc)) from exc


# =================================================================== drift ===
@router.get("/drift")
def get_drift(session: ReadDbSession, principal: CurrentPrincipal, baseline_days: int = 60, window_days: int = 14) -> dict[str, Any]:
    """Compare a recent window against an explicit baseline window."""
    tenant_id = _tenant(principal)
    now = dt.datetime.now(dt.UTC)
    current = (now - dt.timedelta(days=window_days), now)
    baseline = (now - dt.timedelta(days=baseline_days + window_days), now - dt.timedelta(days=window_days))
    report = drift.evaluate_drift(session, tenant_id=tenant_id, baseline_window=baseline, current_window=current)
    payload = report.as_json()
    payload["significant"] = [r.metric for r in report.significant]
    return payload
