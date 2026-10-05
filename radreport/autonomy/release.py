"""Decides, for one report, whether it may be filed with nobody reviewing it.

Order: may_release_without_review checks the grant, the confidence and the findings and returns
a ReleaseDecision; coverage reports how much of a lab's work this currently applies to.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.types import AutonomyStatus, PathType, Severity
from radreport.db.models.review import FinalReport

#: Minimum overall confidence for release with no human review.
#: Deliberately above the 0.70 assistant threshold; tighten, never loosen.
AUTONOMOUS_RELEASE_THRESHOLD = 0.90

#: Verification severities that disqualify a report from autonomous release.
BLOCKING_SEVERITIES = frozenset({Severity.BLOCK, Severity.ERROR})


@dataclass(frozen=True, slots=True)
class AutonomyGrant:
    """A template version's autonomy class, as a stage may see it."""

    class_id: uuid.UUID
    class_code: str
    status: str

    @property
    def is_granted(self) -> bool:
        return self.status == AutonomyStatus.GRANTED


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """Whether this report may skip review, and why not when it may not."""

    eligible: bool
    reason: str
    blocker: str | None = None
    class_code: str | None = None
    class_id: uuid.UUID | None = None


def may_release_without_review(*, grant: AutonomyGrant | None, confidence: float, radiologist_opted_in: bool, has_critical_alert: bool, verification_severities: frozenset[str] = frozenset(), flagged_field_count: int = 0, selected_for_grading: bool = False, threshold: float = AUTONOMOUS_RELEASE_THRESHOLD) -> ReleaseDecision:
    """Every gate, in the order that makes the refusal most informative. Pure."""
    if grant is None:
        return ReleaseDecision(False, "this template has no autonomy class, so there is no evidence to release on", blocker="no_autonomy_class")
    if not grant.is_granted:
        return ReleaseDecision(False, f"autonomy class {grant.class_code!r} is {grant.status}, not granted", blocker="class_not_granted", class_code=grant.class_code, class_id=grant.class_id)

    here = {"class_code": grant.class_code, "class_id": grant.class_id}

    if not radiologist_opted_in:
        # `radiologist_profile.autonomy_enabled`.
        return ReleaseDecision(False, "the dictating radiologist has not enabled autonomy on their profile", blocker="radiologist_not_opted_in", **here)

    if selected_for_grading:
        # The invariant the monitor depends on. See the module docstring: this
        # is not a conservatism, it is what keeps revocation reachable.
        return ReleaseDecision(False, "held back for the grading sample — the CUSUM has no other input, and an unfed monitor reports as coverage it is not providing", blocker="grading_sample", **here)

    if has_critical_alert:
        return ReleaseDecision(False, "a critical finding routes to a radiologist at any confidence", blocker="critical_alert", **here)

    blocking = sorted(verification_severities & BLOCKING_SEVERITIES)
    if blocking:
        return ReleaseDecision(False, f"verification raised {', '.join(blocking)}: a deterministic check on this report outranks a statistical argument about reports like it", blocker="verification_finding", **here)

    if flagged_field_count:
        # A flagged field is a validator that disagreed with the extraction.
        return ReleaseDecision(False, f"{flagged_field_count} flagged field(s) — a validator disagreed with the extraction and nobody would be looking at it", blocker="flagged_fields", **here)

    if confidence < threshold:
        return ReleaseDecision(False, f"confidence {confidence:.2f} below the autonomous-release threshold {threshold:.2f} (the 0.70 is the assistant bar, not this one)", blocker="confidence_below_release_threshold", **here)

    return ReleaseDecision(True, f"autonomy class {grant.class_code!r} is granted and confidence {confidence:.2f} is at or above {threshold:.2f}", **here)


#: the GA target: autonomy removing review from at least this share of volume.
REVIEW_REDUCTION_TARGET = 0.40


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much review autonomy is actually removing."""

    signed: int
    released: int
    since: dt.datetime | None = None

    @property
    def share(self) -> float:
        return round(self.released / self.signed, 4) if self.signed else 0.0

    @property
    def meets_target(self) -> bool:
        return self.share >= REVIEW_REDUCTION_TARGET

    @property
    def reviewed(self) -> int:
        return self.signed - self.released


def coverage(session: Session, *, tenant_id: uuid.UUID, since: dt.datetime | None = None) -> Coverage:
    """Count signed reports and how many went out unreviewed."""
    conditions = [FinalReport.tenant_id == tenant_id, FinalReport.amends_report_id.is_(None)]
    if since is not None:
        conditions.append(FinalReport.signed_at >= since)

    rows = session.execute(select(FinalReport.path_type, func.count()).where(*conditions).group_by(FinalReport.path_type)).all()

    by_path = dict(rows)
    return Coverage(signed=sum(by_path.values()), released=by_path.get(PathType.AUTONOMOUS, 0), since=since)
