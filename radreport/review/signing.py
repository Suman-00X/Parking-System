"""Signing a report, and amending one already signed. Signing is where a draft becomes a legal medical record.

Order: check the gates first (preflight) -> acknowledge any urgent finding (acknowledge_alert)
-> sign (sign_report) -> add an addendum afterwards (amend_report). The gates refuse rather
than warn.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.hashing import canonical_json, hash_text
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, DraftStatus, ExportStatus, PathType, ReviewerRole, Severity
from radreport.db.models.ingestion import Recording
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.reporting import CriticalFindingAlert, ReportDraft, ReportFieldValue, VerificationFinding
from radreport.db.models.review import FinalReport, ReportRevision
from radreport.review.rbac import Permission, Reviewer, require

log = get_logger(__name__)


class SigningRefused(Exception):
    """A gate between a draft and a signed medical record."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class SigningChecks:
    """Everything standing between this draft and a signature."""

    blocking_findings: tuple[str, ...]
    unacknowledged_alerts: tuple[str, ...]
    ungrounded_fields: tuple[str, ...]
    has_revision: bool

    @property
    def may_sign(self) -> bool:
        """Every gate `sign_report` enforces, including the revision one."""
        return self.has_revision and not (self.blocking_findings or self.unacknowledged_alerts or self.ungrounded_fields)


def preflight(session: Session, *, tenant_id: uuid.UUID, draft_id: uuid.UUID) -> SigningChecks:
    """Everything `sign_report` would refuse on, computed without signing."""
    draft = session.get(ReportDraft, draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no report_draft {draft_id} in this tenant")

    blocking = tuple(f.check_id for f in session.execute(select(VerificationFinding).where(VerificationFinding.tenant_id == tenant_id, VerificationFinding.report_draft_id == draft_id, VerificationFinding.severity == Severity.BLOCK)).scalars().all())

    unacknowledged = tuple(str(a.id) for a in session.execute(select(CriticalFindingAlert).where(CriticalFindingAlert.tenant_id == tenant_id, CriticalFindingAlert.recording_id == draft.recording_id, CriticalFindingAlert.acknowledged_at.is_(None))).scalars().all())

    ungrounded = tuple(
        str(v.id)
        for v in session.execute(select(ReportFieldValue).where(ReportFieldValue.tenant_id == tenant_id, ReportFieldValue.report_draft_id == draft_id, ReportFieldValue.is_grounded.is_(False))).scalars().all()
        # A field with no content is a gap, not an ungrounded claim.
        if v.value_text or v.value_enum or v.value_numeric is not None
    )

    has_revision = session.execute(select(ReportRevision).where(ReportRevision.tenant_id == tenant_id, ReportRevision.report_draft_id == draft_id)).first() is not None

    return SigningChecks(blocking_findings=blocking, unacknowledged_alerts=unacknowledged, ungrounded_fields=ungrounded, has_revision=has_revision)


def acknowledge_alert(session: Session, *, tenant_id: uuid.UUID, alert_id: uuid.UUID, reviewer: Reviewer, outcome: str) -> CriticalFindingAlert:
    """Record that a human received an alert and what they made of it."""
    require(reviewer, Permission.ACKNOWLEDGE_ALERT)

    alert = session.get(CriticalFindingAlert, alert_id)
    if alert is None or alert.tenant_id != tenant_id:
        raise ValueError(f"no critical_finding_alert {alert_id} in this tenant")

    now = dt.datetime.now(dt.UTC)
    alert.acknowledged_by = reviewer.user_id
    alert.acknowledged_at = now
    alert.outcome = outcome
    due = alert.sla_due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=dt.UTC)
    alert.is_breach = now > due

    session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer.user_id, actor_type=ActorType.USER, action="critical_alert_acknowledged", entity_type="critical_finding_alert", entity_id=alert.id, after={"outcome": outcome, "is_breach": alert.is_breach}))
    session.flush()

    if alert.is_breach:
        log.warning("critical_alert_sla_breached", alert_id=str(alert.id), due_at=str(alert.sla_due_at), acknowledged_at=str(now))
    return alert


def sign_report(session: Session, *, tenant_id: uuid.UUID, draft_id: uuid.UUID, reviewer: Reviewer, rendered_text: str | None = None) -> FinalReport:
    """Turn a reviewed draft into an immutable signed record."""
    require(reviewer, Permission.SIGN_REPORT)

    draft = session.get(ReportDraft, draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no report_draft {draft_id} in this tenant")
    if draft.status == DraftStatus.SIGNED:
        raise SigningRefused(f"report_draft {draft_id} is already signed; a correction is an addendum, not a re-signature", code="already_signed")

    checks = preflight(session, tenant_id=tenant_id, draft_id=draft_id)
    if checks.blocking_findings:
        raise SigningRefused(f"{len(checks.blocking_findings)} blocking verification finding(s) outstanding: {', '.join(checks.blocking_findings[:3])}", code="blocking_findings")
    if checks.unacknowledged_alerts:
        raise SigningRefused(f"{len(checks.unacknowledged_alerts)} critical alert(s) not yet acknowledged — acknowledge them before signing", code="unacknowledged_alerts")
    if checks.ungrounded_fields:
        raise SigningRefused(f"{len(checks.ungrounded_fields)} field(s) carry a value with no verified provenance (I1); correct or clear them before signing", code="ungrounded_values")

    revision = session.execute(select(ReportRevision).where(ReportRevision.tenant_id == tenant_id, ReportRevision.report_draft_id == draft_id).order_by(ReportRevision.revision_number.desc())).scalars().first()
    if revision is None:
        raise SigningRefused("no revision exists for this draft; a report must be reviewed before it is signed, even when nothing needed changing", code="no_revision")

    recording = session.get(Recording, draft.recording_id)
    if recording is None:
        raise ValueError(f"no recording {draft.recording_id}")

    text = rendered_text if rendered_text is not None else revision.rendered_text
    payload = _structured_payload(session, tenant_id, draft_id)

    # An assistant-reviewed report is one an assistant actually revised.
    roles = {r.reviewer_role for r in session.execute(select(ReportRevision).where(ReportRevision.tenant_id == tenant_id, ReportRevision.report_draft_id == draft_id)).scalars().all()}
    path_type = PathType.TRANSCRIPTIONIST_REVIEWED if ReviewerRole.TRANSCRIPTIONIST in roles else PathType.RADIOLOGIST_ONLY

    now = dt.datetime.now(dt.UTC)
    final = FinalReport(id=uuid.uuid4(), tenant_id=tenant_id, study_id=recording.study_id, report_draft_id=draft_id, final_revision_id=revision.id, signed_by=reviewer.user_id, signed_at=now, rendered_text=text, structured_payload=payload, content_hash=hash_text(canonical_json({"text": text, "fields": payload})), path_type=path_type, export_status=ExportStatus.PENDING)
    session.add(final)
    draft.status = DraftStatus.SIGNED

    session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer.user_id, actor_type=ActorType.USER, action="report_signed", entity_type="final_report", entity_id=final.id, after={"draft_id": str(draft_id), "path_type": path_type, "content_hash": final.content_hash}))
    session.flush()

    log.info("report_signed", final_report_id=str(final.id), draft_id=str(draft_id), path_type=path_type, signed_by=str(reviewer.user_id))
    return final


def amend_report(session: Session, *, tenant_id: uuid.UUID, original_report_id: uuid.UUID, reviewer: Reviewer, rendered_text: str, reason: str) -> FinalReport:
    """Issue an addendum. The original is never modified."""
    require(reviewer, Permission.SIGN_REPORT)

    original = session.get(FinalReport, original_report_id)
    if original is None or original.tenant_id != tenant_id:
        raise ValueError(f"no final_report {original_report_id} in this tenant")

    now = dt.datetime.now(dt.UTC)
    # An addendum is written and signed by a radiologist, so it is never on the autonomous path even when the report it amends was.
    was_autonomous = original.path_type == PathType.AUTONOMOUS
    addendum = FinalReport(id=uuid.uuid4(), tenant_id=tenant_id, study_id=original.study_id, report_draft_id=original.report_draft_id, final_revision_id=original.final_revision_id, signed_by=reviewer.user_id, signed_at=now, rendered_text=rendered_text, structured_payload=original.structured_payload, content_hash=hash_text(canonical_json({"text": rendered_text, "amends": str(original.id)})), path_type=PathType.RADIOLOGIST_ONLY if was_autonomous else original.path_type, amends_report_id=original.id, export_status=ExportStatus.PENDING)
    session.add(addendum)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=reviewer.user_id,
            actor_type=ActorType.USER,
            action="report_amended",
            entity_type="final_report",
            entity_id=addendum.id,
            before={"original_report_id": str(original.id), "original_path_type": original.path_type},
            after={
                "reason": reason,
                "content_hash": addendum.content_hash,
                "path_type": addendum.path_type,
                # The audit question this answers: did a report that went out unreviewed later need correcting?
                "amends_autonomous_release": was_autonomous,
            },
        )
    )
    session.flush()

    log.warning("report_amended", addendum_id=str(addendum.id), amends=str(original.id), amends_autonomous_release=was_autonomous, reason=reason)
    if was_autonomous:
        # Loud, separately, because this is an autonomy *outcome* and not just a correction: a released report that needed an addendum is the clearest evidence the release was wrong.
        log.warning("autonomous_release_amended", addendum_id=str(addendum.id), original_report_id=str(original.id), autonomy_class_id=str(original.autonomy_class_id) if original.autonomy_class_id else None, detail=("a report released without review required an addendum; grade the original so the CUSUM sees it"))
    return addendum


def _structured_payload(session: Session, tenant_id: uuid.UUID, draft_id: uuid.UUID) -> dict[str, object]:
    values = session.execute(select(ReportFieldValue).where(ReportFieldValue.tenant_id == tenant_id, ReportFieldValue.report_draft_id == draft_id, ReportFieldValue.is_grounded.is_(True))).scalars().all()
    return {str(v.template_field_id): {"value_text": v.value_text, "value_enum": v.value_enum, "value_numeric": float(v.value_numeric) if v.value_numeric is not None else None, "value_unit": v.value_unit, "assertion_status": v.assertion_status, "laterality": v.laterality, "fill_source": v.fill_source} for v in values}
