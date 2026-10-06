"""Grades a sample of signed reports on how clinically serious the reviewer's corrections were, not how many there were.

Order: grade one report (grade_report) -> report the rate of clinically significant errors
across a lab (cse_rate).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import CSE_GRADES, ActorType, SeverityGrade
from radreport.db.models.knowledge import AutonomyClass
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.review import EditEvent, FinalReport
from radreport.review.rbac import Permission, Reviewer, require

log = get_logger(__name__)

#: the descriptions, carried in code so the UI and the analysis cannot
#: drift into two different scales.
GRADE_DESCRIPTIONS: dict[str, str] = {SeverityGrade.G0: "No change needed.", SeverityGrade.G1: "Style or formatting only. No change in meaning.", SeverityGrade.G2: "Clarity improved. Meaning unchanged for a clinician.", SeverityGrade.G3: "Clinically significant: a clinician could act differently.", SeverityGrade.G4: "Potentially harmful: could lead to patient harm."}


@dataclass(frozen=True, slots=True)
class GradeResult:
    final_report_id: uuid.UUID
    grade: str
    is_cse: bool
    previous_grade: str | None
    edit_events_graded: int

    accrued: bool = False
    """Whether this grade became autonomy evidence."""

    autonomy_revoked: bool = False
    """The CUSUM crossed its threshold on this grade and autonomy came off."""


def grade_report(session: Session, *, tenant_id: uuid.UUID, final_report_id: uuid.UUID, grade: str, reviewer: Reviewer, note: str | None = None) -> GradeResult:
    """Record a G0–G4 grade against a signed report."""
    require(reviewer, Permission.GRADE_REPORT)
    if grade not in SeverityGrade.values():
        raise ValueError(f"unknown severity grade {grade!r}; expected one of G0–G4")

    final = session.get(FinalReport, final_report_id)
    if final is None or final.tenant_id != tenant_id:
        raise ValueError(f"no final_report {final_report_id} in this tenant")

    # An autonomously released report has no revision and therefore no edit events.
    events: list[EditEvent] = []
    if final.final_revision_id is not None:
        events = list(session.execute(select(EditEvent).where(EditEvent.tenant_id == tenant_id, EditEvent.report_revision_id == final.final_revision_id)).scalars().all())
    previous = next((e.severity_grade for e in events if e.severity_grade), None)

    for event in events:
        event.severity_grade = grade

    is_cse = grade in CSE_GRADES

    # Close the loop.
    accrued, revoked = _feed_autonomy(session, tenant_id, final, grade)

    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=reviewer.user_id,
            actor_type=ActorType.USER,
            action="report_graded" if previous is None else "report_regraded",
            entity_type="final_report",
            entity_id=final.id,
            # Both values, always. A CSE rate that can be quietly adjusted is
            # not evidence in a safety argument.
            before={"grade": previous} if previous else None,
            after={"grade": grade, "is_cse": is_cse, "note": note},
        )
    )
    session.flush()

    log.info("report_graded", final_report_id=str(final.id), grade=grade, is_cse=is_cse, regrade=previous is not None, edit_events=len(events))
    return GradeResult(final_report_id=final.id, grade=grade, is_cse=is_cse, previous_grade=previous, edit_events_graded=len(events), accrued=accrued, autonomy_revoked=revoked)


def _feed_autonomy(session: Session, tenant_id: uuid.UUID, final: FinalReport, grade: str) -> tuple[bool, bool]:
    """Record the observation and advance the CUSUM. Returns `(accrued, revoked)`."""
    from radreport.autonomy import accrual, grant

    try:
        observation = accrual.record_observation(session, tenant_id=tenant_id, final_report_id=final.id, severity_grade=grade)
    except Exception as exc:  # noqa: BLE001 - the grade must still be saved
        log.warning("autonomy_accrual_failed", final_report_id=str(final.id), error=f"{type(exc).__name__}: {exc}")
        return False, False

    if observation is None:
        return False, False

    final_class = accrual.class_for_report(session, tenant_id, final)
    if final_class is None:
        return True, False
    class_code = final_class.code

    try:
        step = grant.observe_graded_report(session, tenant_id=tenant_id, class_code=class_code, severity_grade=grade)
    except Exception as exc:  # noqa: BLE001
        log.warning("autonomy_monitor_failed", class_code=class_code, error=f"{type(exc).__name__}: {exc}")
        return True, False

    if step.signalled:
        log.warning("autonomy_revoked_by_grading", class_code=class_code, final_report_id=str(final.id), cusum=step.statistic)
    return observation.counted_in_accrual, step.signalled


@dataclass(frozen=True, slots=True)
class CseRate:
    """The measured rate, with the denominator it was measured over."""

    graded: int
    cse_count: int

    @property
    def rate(self) -> float:
        return round(self.cse_count / self.graded, 5) if self.graded else 0.0

    @property
    def is_reportable(self) -> bool:
        """Below ~100 graded reports the rate is noise around a ~2.5% baseline."""
        return self.graded >= 100


def cse_rate(session: Session, *, tenant_id: uuid.UUID, autonomy_class_code: str | None = None) -> CseRate:
    """Observed CSE rate — the input to the non-inferiority test."""
    stmt = select(EditEvent.severity_grade, func.count()).where(EditEvent.tenant_id == tenant_id, EditEvent.severity_grade.isnot(None))
    rows = session.execute(stmt.group_by(EditEvent.severity_grade)).all()

    graded = sum(count for _grade, count in rows)
    cse_count = sum(count for grade, count in rows if grade in CSE_GRADES)

    if autonomy_class_code is not None:
        # Scoped rates need the class join; unscoped is the whole-tenant view.
        klass = session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id, AutonomyClass.code == autonomy_class_code)).scalar_one_or_none()
        if klass is None:
            raise ValueError(f"no autonomy_class {autonomy_class_code!r} in this tenant")

    return CseRate(graded=graded, cse_count=cse_count)
