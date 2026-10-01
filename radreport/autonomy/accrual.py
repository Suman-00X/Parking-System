"""Builds the evidence for letting a class of reports skip review, by watching what reviewers change -- without acting on it.

Order: record what a reviewer did to each draft (record_observation) -> summarise the record so
far (snapshot) -> test whether the unreviewed rate would be no worse than the reviewed one
(posterior_non_inferiority) -> start collecting for a new class (open_accrual).
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import CSE_GRADES, ActorType, AutonomyStatus, SeverityGrade
from radreport.db.models.evaluation import EvalItem
from radreport.db.models.knowledge import AutonomyClass, Template, TemplateVersion
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.reporting import AutonomyObservation, ReportDraft
from radreport.db.models.review import FinalReport

log = get_logger(__name__)

#: A Bayesian sequential design needs a posterior this high before a
#: grant is even arguable; granting itself is a separate, deliberate step.
POSTERIOR_THRESHOLD = 0.95

#: Beta's Jeffreys prior on the CSE rate. Weakly informative on purpose: a
#: stronger prior would let the first hundred clean reports carry the argument.
_PRIOR_ALPHA = 0.5
_PRIOR_BETA = 0.5


@dataclass(frozen=True, slots=True)
class AccrualSnapshot:
    """What the evidence currently says. Never what to do about it."""

    class_code: str
    baseline_cse_rate: float
    ni_margin_pp: float
    required_n: int
    graded_n: int
    observed_cse: int
    posterior_prob_ni: float | None

    @property
    def observed_rate(self) -> float:
        return round(self.observed_cse / self.graded_n, 5) if self.graded_n else 0.0

    @property
    def meets_volume(self) -> bool:
        return self.graded_n >= self.required_n

    @property
    def would_support_grant(self) -> bool:
        """Whether the evidence *would* support a grant, if granting existed."""
        return self.meets_volume and self.posterior_prob_ni is not None and self.posterior_prob_ni >= POSTERIOR_THRESHOLD


def record_observation(session: Session, *, tenant_id: uuid.UUID, final_report_id: uuid.UUID, severity_grade: str) -> AutonomyObservation | None:
    """Record one **graded** signed report against its autonomy class."""
    if severity_grade not in SeverityGrade.values():
        raise ValueError(f"unknown severity grade {severity_grade!r}")

    final = session.get(FinalReport, final_report_id)
    if final is None or final.tenant_id != tenant_id:
        raise ValueError(f"no final_report {final_report_id} in this tenant")

    klass = _class_for_report(session, tenant_id, final)
    if klass is None:
        return None
    if klass.status == AutonomyStatus.NOT_EVALUATED:
        log.info("autonomy_observation_skipped", final_report_id=str(final.id), reason="class is not_evaluated; accrual has not been opened")
        return None

    existing = session.execute(select(AutonomyObservation).where(AutonomyObservation.tenant_id == tenant_id, AutonomyObservation.final_report_id == final_report_id)).scalar_one_or_none()
    if existing is not None:
        # A regrade corrects the evidence rather than adding to it.
        _apply_regrade(klass, existing, severity_grade)
        session.flush()
        return existing

    is_cse = severity_grade in CSE_GRADES
    counted = _counts_toward_accrual(session, tenant_id, final)

    observation = AutonomyObservation(tenant_id=tenant_id, autonomy_class_id=klass.id, final_report_id=final.id, severity_grade=severity_grade, is_cse=is_cse, counted_in_accrual=counted, observed_at=dt.datetime.now(dt.UTC))
    session.add(observation)

    if counted:
        klass.accrued_n = (klass.accrued_n or 0) + 1
        if is_cse:
            klass.observed_cse_count = (klass.observed_cse_count or 0) + 1

    session.flush()
    log.info("autonomy_observation_recorded", class_code=klass.code, grade=severity_grade, is_cse=is_cse, counted_in_accrual=counted, accrued_n=klass.accrued_n)
    return observation


def _apply_regrade(klass: AutonomyClass, observation: AutonomyObservation, severity_grade: str) -> None:
    """Correct an existing observation in place, adjusting the class counters."""
    was_cse = observation.is_cse
    now_cse = severity_grade in CSE_GRADES

    observation.severity_grade = severity_grade
    observation.is_cse = now_cse

    if observation.counted_in_accrual and was_cse != now_cse:
        delta = 1 if now_cse else -1
        klass.observed_cse_count = max(0, (klass.observed_cse_count or 0) + delta)


def _counts_toward_accrual(session: Session, tenant_id: uuid.UUID, final: FinalReport) -> bool:
    """False when the report's audio is in an eval set."""
    recording_id = session.execute(select(ReportDraft.recording_id).where(ReportDraft.tenant_id == tenant_id, ReportDraft.id == final.report_draft_id)).scalars().first()
    if recording_id is None:
        return True

    in_eval_set = session.execute(select(EvalItem.id).where(EvalItem.recording_id == recording_id).limit(1)).scalars().first()
    return in_eval_set is None


def snapshot(session: Session, *, tenant_id: uuid.UUID, class_code: str) -> AccrualSnapshot:
    """Current evidence for one class. Read-only; grants nothing."""
    klass = session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id, AutonomyClass.code == class_code)).scalar_one_or_none()
    if klass is None:
        raise ValueError(f"no autonomy_class {class_code!r} in this tenant")

    baseline = float(klass.baseline_cse_rate or 0.0)
    if baseline <= 0:
        #  has not produced a baseline. Computing a posterior against zero
        # would return a confident answer about a number nobody measured.
        raise ValueError(f"autonomy_class {class_code!r} has no measured baseline_cse_rate; the audit must grade 50 signed reports first")

    graded, cse = _graded_counts(session, tenant_id, klass.id)
    margin = float(klass.ni_margin_pp or 1.0) / 100.0
    posterior = posterior_non_inferiority(graded=graded, cse=cse, baseline=baseline, margin=margin)

    klass.posterior_prob_ni = posterior
    session.flush()

    return AccrualSnapshot(class_code=klass.code, baseline_cse_rate=baseline, ni_margin_pp=float(klass.ni_margin_pp or 1.0), required_n=klass.required_n, graded_n=graded, observed_cse=cse, posterior_prob_ni=posterior)


def posterior_non_inferiority(*, graded: int, cse: int, baseline: float, margin: float) -> float | None:
    """P(true CSE rate < baseline + margin), under a Beta-Binomial model."""
    if graded < 30:
        return None

    alpha = _PRIOR_ALPHA + cse
    beta = _PRIOR_BETA + (graded - cse)
    threshold = min(1.0, baseline + margin)
    return round(_beta_cdf(threshold, alpha, beta), 5)


def _beta_cdf(x: float, alpha: float, beta: float) -> float:
    """Regularised incomplete beta function I_x(a, b) — the Beta CDF."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    log_front = math.lgamma(alpha + beta) - math.lgamma(alpha) - math.lgamma(beta) + alpha * math.log(x) + beta * math.log(1 - x)
    front = math.exp(log_front)

    # The fraction converges quickly only on one side of this point; the
    # symmetry I_x(a,b) = 1 - I_{1-x}(b,a) covers the other.
    if x < (alpha + 1) / (alpha + beta + 2):
        return front * _betacf(alpha, beta, x) / alpha
    return 1.0 - front * _betacf(beta, alpha, 1 - x) / beta


def _betacf(a: float, b: float, x: float, *, max_iterations: int = 200) -> float:
    """Continued fraction for the incomplete beta, by modified Lentz."""
    tiny = 1e-30
    epsilon = 1e-12

    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d

    for m in range(1, max_iterations + 1):
        m2 = 2 * m

        # Even step.
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c

        # Odd step.
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < epsilon:
            break

    return h


def open_accrual(session: Session, *, tenant_id: uuid.UUID, class_code: str, actor_id: uuid.UUID | None = None) -> AutonomyClass:
    """Move a class from `not_evaluated` to `accruing`. **Grants nothing.**"""
    klass = session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id, AutonomyClass.code == class_code)).scalar_one_or_none()
    if klass is None:
        raise ValueError(f"no autonomy_class {class_code!r} in this tenant")
    if not klass.baseline_cse_rate or float(klass.baseline_cse_rate) <= 0:
        raise ValueError("cannot open accrual without a measured baseline_cse_rate: every non-inferiority calculation would be against an assumed number ")

    previous = klass.status
    klass.status = AutonomyStatus.ACCRUING
    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="autonomy_accrual_opened", entity_type="autonomy_class", entity_id=klass.id, before={"status": previous}, after={"status": klass.status, "baseline_cse_rate": float(klass.baseline_cse_rate), "note": "observation only; granting is a separate step"}))
    session.flush()
    log.info("autonomy_accrual_opened", class_code=class_code, previous=previous)
    return klass


def _class_for_report(session: Session, tenant_id: uuid.UUID, final: FinalReport) -> AutonomyClass | None:
    row = session.execute(select(AutonomyClass).join(Template, Template.autonomy_class_id == AutonomyClass.id).join(TemplateVersion, TemplateVersion.template_id == Template.id).join(ReportDraft, ReportDraft.template_version_id == TemplateVersion.id).where(ReportDraft.tenant_id == tenant_id, ReportDraft.id == final.report_draft_id)).scalars().first()
    return row


def _graded_counts(session: Session, tenant_id: uuid.UUID, class_id: uuid.UUID) -> tuple[int, int]:
    """`(graded, cse)` — graded reports only."""
    grades = list(
        session.execute(
            select(AutonomyObservation.severity_grade).where(
                AutonomyObservation.tenant_id == tenant_id,
                AutonomyObservation.autonomy_class_id == class_id,
                # Not `severity_grade IS NOT NULL` — the column is NOT NULL, so that filter matched everything.
                AutonomyObservation.counted_in_accrual.is_(True),
            )
        )
        .scalars()
        .all()
    )
    return len(grades), sum(1 for g in grades if g in CSE_GRADES)
