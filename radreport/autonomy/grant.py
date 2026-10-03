"""Grants a class of reports the right to skip review, and takes it away when quality slips.

Order: grant the permission once the evidence holds (grant) -> watch each graded report
afterwards (observe_graded_report, cusum_increment, step_cusum) -> withdraw it on a sustained
rise in errors (revoke) or pause it (suspend).
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.autonomy.accrual import POSTERIOR_THRESHOLD, snapshot
from radreport.core.logging import get_logger
from radreport.core.types import CSE_GRADES, ActorType, AutonomyStatus
from radreport.db.models.knowledge import AutonomyClass
from radreport.db.models.orchestration import AuditLog

log = get_logger(__name__)

#: Degraded rate the CUSUM is tuned to detect, as a multiple of baseline (2×, not a fixed percentage).
DEGRADED_RATE_MULTIPLE = 2.0

#: Default decision interval h, from simulated average run lengths (mean graded
#: reports until the monitor signals). `tests/unit/test_grant.py` re-derives this:
#:
#:     h     ARL @2.5% (in control)   ARL @5% (doubled)   ARL @7.5%
#:     2.0                      776                 138          66
#:     3.0                     2448                 233         102
#:     4.0                     4291                 326         138
#:     5.0                     5335                 427         174
#:
#: 3.0 is chosen: in-control ARL ~2450 is comparable to the ~3000-report accrual.
DEFAULT_CUSUM_THRESHOLD = 3.0


class GrantRefused(Exception):
    """A precondition for granting autonomy was not met."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class CusumStep:
    statistic: float
    threshold: float
    increment: float
    signalled: bool


def cusum_increment(*, is_cse: bool, acceptable: float, degraded: float) -> float:
    """Log-likelihood ratio for one graded report."""
    acceptable = min(max(acceptable, 1e-6), 1 - 1e-6)
    degraded = min(max(degraded, 1e-6), 1 - 1e-6)
    if is_cse:
        return math.log(degraded / acceptable)
    return math.log((1 - degraded) / (1 - acceptable))


def step_cusum(*, current: float, is_cse: bool, baseline: float, threshold: float, degraded_multiple: float = DEGRADED_RATE_MULTIPLE) -> CusumStep:
    """Advance the CUSUM by one graded report."""
    degraded = min(0.99, baseline * degraded_multiple)
    increment = cusum_increment(is_cse=is_cse, acceptable=baseline, degraded=degraded)
    statistic = max(0.0, current + increment)
    return CusumStep(statistic=round(statistic, 4), threshold=threshold, increment=round(increment, 4), signalled=statistic >= threshold)


def grant(session: Session, *, tenant_id: uuid.UUID, class_code: str, platform_user_id: uuid.UUID, cusum_threshold: float = DEFAULT_CUSUM_THRESHOLD) -> AutonomyClass:
    """Grant autonomy to a class. **Product-admin only**."""
    klass = _load(session, tenant_id, class_code)
    if klass.status == AutonomyStatus.GRANTED:
        raise GrantRefused(f"autonomy_class {class_code!r} is already granted", code="already_granted")
    if klass.status == AutonomyStatus.REVOKED:
        raise GrantRefused(f"autonomy_class {class_code!r} was revoked; re-granting requires a new accrual period, not a re-grant of the evidence that was already judged", code="previously_revoked")

    evidence = snapshot(session, tenant_id=tenant_id, class_code=class_code)
    if not evidence.meets_volume:
        raise GrantRefused(f"{evidence.graded_n} graded reports against a required {evidence.required_n}; a posterior from too few observations is confident about the wrong thing", code="insufficient_volume")
    if evidence.posterior_prob_ni is None or evidence.posterior_prob_ni < POSTERIOR_THRESHOLD:
        raise GrantRefused(f"P(non-inferior) = {evidence.posterior_prob_ni} is below {POSTERIOR_THRESHOLD}", code="posterior_below_threshold")

    previous = klass.status
    klass.status = AutonomyStatus.GRANTED
    klass.granted_at = dt.datetime.now(dt.UTC)
    klass.granted_by = platform_user_id
    # The monitor starts clean and starts *now*. Carrying a statistic over from
    # the accrual period would revoke on evidence the grant already weighed.
    klass.cusum_statistic = 0.0
    klass.cusum_threshold = cusum_threshold

    session.add(AuditLog(tenant_id=tenant_id, actor_id=platform_user_id, actor_type=ActorType.USER, action="autonomy_granted", entity_type="autonomy_class", entity_id=klass.id, before={"status": previous}, after={"status": klass.status, "graded_n": evidence.graded_n, "observed_cse": evidence.observed_cse, "observed_rate": evidence.observed_rate, "baseline_cse_rate": evidence.baseline_cse_rate, "posterior_prob_ni": evidence.posterior_prob_ni, "cusum_threshold": cusum_threshold}))
    session.flush()

    log.warning("autonomy_granted", class_code=class_code, graded_n=evidence.graded_n, observed_rate=evidence.observed_rate, posterior=evidence.posterior_prob_ni, granted_by=str(platform_user_id))
    return klass


def observe_graded_report(session: Session, *, tenant_id: uuid.UUID, class_code: str, severity_grade: str) -> CusumStep:
    """Feed one graded report to the monitor, revoking if it signals."""
    klass = _load(session, tenant_id, class_code)
    if klass.status != AutonomyStatus.GRANTED:
        # Only a granted class is monitored. An accruing one is gathering
        # evidence for a decision nobody has made yet.
        return CusumStep(statistic=float(klass.cusum_statistic or 0.0), threshold=float(klass.cusum_threshold or DEFAULT_CUSUM_THRESHOLD), increment=0.0, signalled=False)

    baseline = float(klass.baseline_cse_rate or 0.0)
    if baseline <= 0:
        raise ValueError(f"autonomy_class {class_code!r} is granted with no measured baseline; the monitor has nothing to compare against")

    step = step_cusum(current=float(klass.cusum_statistic or 0.0), is_cse=severity_grade in CSE_GRADES, baseline=baseline, threshold=float(klass.cusum_threshold or DEFAULT_CUSUM_THRESHOLD))
    klass.cusum_statistic = step.statistic
    session.flush()

    if step.signalled:
        revoke(session, tenant_id=tenant_id, class_code=class_code, reason=(f"CUSUM {step.statistic} reached its threshold {step.threshold}: the accumulated evidence favours a CSE rate near {baseline * DEGRADED_RATE_MULTIPLE:.3%} over the baseline {baseline:.3%}"), actor_id=None)
    return step


def revoke(session: Session, *, tenant_id: uuid.UUID, class_code: str, reason: str, actor_id: uuid.UUID | None = None) -> AutonomyClass:
    """Withdraw autonomy. Mechanical when the CUSUM fires; always available."""
    klass = _load(session, tenant_id, class_code)
    previous = klass.status

    klass.status = AutonomyStatus.REVOKED
    klass.revoked_at = dt.datetime.now(dt.UTC)
    klass.revocation_reason = reason

    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_type=ActorType.USER if actor_id else ActorType.SYSTEM,
            action="autonomy_revoked",
            entity_type="autonomy_class",
            entity_id=klass.id,
            before={"status": previous, "cusum_statistic": float(klass.cusum_statistic or 0)},
            # The statistic that caused it, in the record. A grant withdrawn
            # without a traceable reason is indistinguishable from a bug.
            after={"status": klass.status, "reason": reason},
        )
    )
    session.flush()

    log.warning("autonomy_revoked", class_code=class_code, previous=previous, reason=reason, automatic=actor_id is None)
    return klass


def suspend(session: Session, *, tenant_id: uuid.UUID, class_code: str, reason: str, actor_id: uuid.UUID | None = None) -> AutonomyClass:
    """Pause autonomy without discarding the accrued evidence."""
    klass = _load(session, tenant_id, class_code)
    previous = klass.status
    klass.status = AutonomyStatus.SUSPENDED

    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="autonomy_suspended", entity_type="autonomy_class", entity_id=klass.id, before={"status": previous}, after={"status": klass.status, "reason": reason}))
    session.flush()
    log.warning("autonomy_suspended", class_code=class_code, reason=reason)
    return klass


def _load(session: Session, tenant_id: uuid.UUID, class_code: str) -> AutonomyClass:
    klass = session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id, AutonomyClass.code == class_code)).scalar_one_or_none()
    if klass is None:
        raise ValueError(f"no autonomy_class {class_code!r} in this tenant")
    return klass
