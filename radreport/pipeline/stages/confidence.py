"""Stage 14: works out how much to trust the draft, as one number.

Order: compute_confidence combines the earlier stages' scores into a ConfidenceBreakdown;
needs_radiologist says whether that number is too low to go to anyone but a radiologist.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from radreport.core.logging import get_logger
from radreport.core.types import Severity
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import PipelineState

log = get_logger(__name__)

#: Below this, a draft goes to a radiologist rather than an assistant.
#: A routing threshold, not a quality claim.
RADIOLOGIST_REVIEW_THRESHOLD = 0.70


@dataclass(slots=True)
class ConfidenceBreakdown:
    """Why the number is what it is. The review UI shows this, not the scalar."""

    overall: float
    critical_min: float | None
    all_mean: float
    field_count: int
    critical_field_count: int
    ungrounded_count: int
    blocked: bool = False
    weakest_field: str | None = None
    contributors: dict[str, float] = field(default_factory=dict)


def compute_confidence(state: PipelineState, *, critical_field_keys: frozenset[str] = frozenset()) -> ConfidenceBreakdown:
    """The formula, with the two honesty rules applied. Pure."""
    values = state.field_values
    if not values:
        return ConfidenceBreakdown(overall=0.0, critical_min=None, all_mean=0.0, field_count=0, critical_field_count=0, ungrounded_count=0, blocked=_has_blocking(state))

    # An ungrounded field scores 0 rather than being excluded. Excluding it
    # would let a report whose extraction largely failed score on the remainder.
    scores = {key: (value.confidence if value.is_grounded else 0.0) for key, value in values.items()}
    ungrounded = sum(1 for value in values.values() if not value.is_grounded)

    critical_scores = {k: v for k, v in scores.items() if k in critical_field_keys}
    all_mean = round(statistics.fmean(scores.values()), 4)
    critical_min = round(min(critical_scores.values()), 4) if critical_scores else None

    overall = all_mean if critical_min is None else round(critical_min * all_mean, 4)

    blocked = _has_blocking(state)
    if blocked:
        # A draft that contradicts itself has no meaningful confidence. Any
        # positive number invites someone to act on it.
        overall = 0.0

    weakest = min(scores, key=lambda k: (scores[k], k)) if scores else None

    return ConfidenceBreakdown(overall=overall, critical_min=critical_min, all_mean=all_mean, field_count=len(scores), critical_field_count=len(critical_scores), ungrounded_count=ungrounded, blocked=blocked, weakest_field=weakest, contributors=dict(sorted(scores.items())))


def _has_blocking(state: PipelineState) -> bool:
    return any(f.severity == Severity.BLOCK for f in state.verification)


def needs_radiologist(breakdown: ConfidenceBreakdown, *, threshold: float = RADIOLOGIST_REVIEW_THRESHOLD, has_critical_alert: bool = False) -> bool:
    """Stage 15's routing decision."""
    return has_critical_alert or breakdown.blocked or breakdown.overall < threshold


class ConfidenceStage:
    """Arithmetic over state; no model, no I/O."""

    name = "confidence"
    version = "1.0.0"

    def __init__(self, *, critical_field_keys: frozenset[str] = frozenset()) -> None:
        self._critical_field_keys = critical_field_keys

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        breakdown = compute_confidence(state, critical_field_keys=self._critical_field_keys)
        state.overall_confidence = breakdown.overall

        warnings: list[str] = []
        if breakdown.critical_min is None and state.field_values:
            warnings.append("no fields are marked is_critical, so the min safeguard is inactive — Pass 1 has not been run for this template")
        if breakdown.blocked:
            warnings.append("confidence forced to 0 by a blocking verification finding")

        log.info("confidence_computed", overall=breakdown.overall, critical_min=breakdown.critical_min, all_mean=breakdown.all_mean, ungrounded=breakdown.ungrounded_count, weakest_field=breakdown.weakest_field, needs_radiologist=needs_radiologist(breakdown, has_critical_alert=bool(state.critical_alerts)))
        return StageResult(output=state, confidence=breakdown.overall, warnings=warnings)
