"""Stage 15: decides who reviews the draft -- a radiologist, an assistant, or nobody.

Order: decide weighs confidence, findings and the reviewer's role; grading_rate and
is_sampled_for_grading pull a steady sample for quality grading regardless of that decision.
"""

from __future__ import annotations

import hashlib
import uuid

from radreport.autonomy.release import AUTONOMOUS_RELEASE_THRESHOLD, AutonomyGrant, may_release_without_review
from radreport.core.logging import get_logger
from radreport.core.types import PathType, ReviewerRole, Severity, StudyPriority
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.confidence import RADIOLOGIST_REVIEW_THRESHOLD, compute_confidence
from radreport.pipeline.stages.providers import KnowledgeProvider
from radreport.pipeline.state import PipelineState, RoutingToHuman

log = get_logger(__name__)

__all__ = ["DEFAULT_GRADING_RATE", "GRADING_SCHEDULE", "RouteToHumanStage", "RoutingToHuman", "decide", "grading_rate", "is_sampled_for_grading"]

#: the sampling schedule: (weeks_since_go_live_exclusive_upper, 1-in-N).
GRADING_SCHEDULE: tuple[tuple[int, int], ...] = (
    (4, 1),  # weeks 1–4: every report
    (8, 5),  # then 1 in 5
    (12, 10),  # then 1 in 10
)
DEFAULT_GRADING_RATE = 20  # steady state: 1 in 20


def grading_rate(weeks_since_go_live: int) -> int:
    """1-in-N for this point in the schedule."""
    for upper, rate in GRADING_SCHEDULE:
        if weeks_since_go_live < upper:
            return rate
    return DEFAULT_GRADING_RATE


def is_sampled_for_grading(recording_id: uuid.UUID, *, rate: int) -> bool:
    """Deterministic 1-in-N on the recording id."""
    if rate <= 1:
        return True
    digest = hashlib.sha256(recording_id.bytes).digest()
    return int.from_bytes(digest[:8], "big") % rate == 0


def decide(state: PipelineState, *, critical_field_keys: frozenset[str] = frozenset(), threshold: float = RADIOLOGIST_REVIEW_THRESHOLD, weeks_since_go_live: int = 0, study_priority: str = StudyPriority.ROUTINE, autonomy: AutonomyGrant | None = None, release_threshold: float = AUTONOMOUS_RELEASE_THRESHOLD) -> RoutingToHuman:
    """Who reviews this draft, or that nobody does. Pure."""
    breakdown = compute_confidence(state, critical_field_keys=critical_field_keys)
    has_alert = bool(state.critical_alerts)
    has_block = any(f.severity == Severity.BLOCK for f in state.verification)
    sampled = is_sampled_for_grading(state.recording_id, rate=grading_rate(weeks_since_go_live))

    if has_alert:
        role, reason = ReviewerRole.RADIOLOGIST, "critical finding detected"
    elif has_block:
        role, reason = (ReviewerRole.RADIOLOGIST, "blocking verification finding — the draft contradicts its source")
    elif breakdown.overall < threshold:
        role, reason = (ReviewerRole.RADIOLOGIST, f"confidence {breakdown.overall:.2f} below {threshold:.2f}")
    else:
        role, reason = (ReviewerRole.TRANSCRIPTIONIST, f"confidence {breakdown.overall:.2f} at or above {threshold:.2f}")

    priority = study_priority
    if has_alert and study_priority == StudyPriority.ROUTINE:
        # requires priority ordering in the queue. A critical finding
        # promotes the draft even though the alert itself already bypassed it.
        priority = StudyPriority.URGENT

    # Only ever consulted on the assistant path.
    release = may_release_without_review(grant=autonomy, confidence=breakdown.overall, radiologist_opted_in=bool(state.radiologist is not None and state.radiologist.autonomy_enabled), has_critical_alert=has_alert, verification_severities=frozenset(f.severity for f in state.verification), flagged_field_count=sum(1 for v in state.field_values.values() if v.is_flagged), selected_for_grading=sampled, threshold=release_threshold)
    released = release.eligible and role == ReviewerRole.TRANSCRIPTIONIST

    if released:
        return RoutingToHuman(reviewer_role=None, path_type=PathType.AUTONOMOUS, priority=priority, reason=release.reason, selected_for_grading=sampled, confidence=breakdown.overall, released_without_review=True, autonomy_class_code=release.class_code)

    return RoutingToHuman(
        reviewer_role=role,
        path_type=(PathType.RADIOLOGIST_ONLY if role == ReviewerRole.RADIOLOGIST else PathType.TRANSCRIPTIONIST_REVIEWED),
        priority=priority,
        reason=reason,
        selected_for_grading=sampled,
        confidence=breakdown.overall,
        released_without_review=False,
        # Why this report still needed a human, even when the reason was the routing rule rather than an autonomy gate: the target is a percentage of volume, and you cannot raise a percentage you cannot attribute.
        release_blocker=(release.blocker if release.blocker is not None else "routed_to_radiologist"),
        autonomy_class_code=release.class_code,
    )


class RouteToHumanStage:
    """Pure decision over state."""

    name = "route_to_human"
    version = "1.0.0"

    def __init__(self, *, critical_field_keys: frozenset[str] = frozenset(), threshold: float = RADIOLOGIST_REVIEW_THRESHOLD, weeks_since_go_live: int = 0, knowledge: KnowledgeProvider | None = None, release_threshold: float = AUTONOMOUS_RELEASE_THRESHOLD) -> None:
        self._critical_field_keys = critical_field_keys
        self._threshold = threshold
        self._weeks = weeks_since_go_live
        self._knowledge = knowledge
        self._release_threshold = release_threshold

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        # No provider bound ⇒ no autonomy, so the stage behaves as it did before unreviewed release existed.
        autonomy = None
        if self._knowledge is not None and state.routing is not None:
            version_id = state.routing.chosen_template_version_id
            if version_id is not None:
                autonomy = self._knowledge.for_tenant(state.tenant_id).autonomy.get(version_id)

        outcome = decide(state, critical_field_keys=self._critical_field_keys, threshold=self._threshold, weeks_since_go_live=self._weeks, autonomy=autonomy, release_threshold=self._release_threshold)
        state.overall_confidence = outcome.confidence
        state.human_routing = outcome

        if outcome.released_without_review:
            # `warning`, not `info`.
            log.warning("released_without_review", recording_id=str(state.recording_id), autonomy_class=outcome.autonomy_class_code, confidence=outcome.confidence, reason=outcome.reason)
        else:
            log.info("routed_to_human", reviewer_role=outcome.reviewer_role, path_type=outcome.path_type, priority=outcome.priority, reason=outcome.reason, selected_for_grading=outcome.selected_for_grading, release_blocker=outcome.release_blocker, confidence=outcome.confidence)
        return StageResult(output=state, confidence=outcome.confidence, warnings=[outcome.reason] if outcome.reviewer_role == ReviewerRole.RADIOLOGIST else [])
