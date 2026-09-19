"""Decides whether a measured number is good enough to let a change ship.

Order: evaluate_gate compares a GateMetric against its threshold in the required Direction and
returns a GateVerdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from radreport.db.models.evaluation import EvalRun


class Direction(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


@dataclass(frozen=True, slots=True)
class GateMetric:
    key: str
    direction: Direction
    tolerance: float = 0.0
    """Absolute slack. Non-zero admits run-to-run noise; zero means any regression fails."""

    require_breakdowns: bool = False
    """Check per-group values, not just the headline."""


#: the three named gate metrics, plus the ASR pair.
RELEASE_GATE_METRICS: tuple[GateMetric, ...] = (GateMetric("CSE_DRAFT", Direction.LOWER_IS_BETTER, tolerance=0.0), GateMetric("HALLUC_RATE", Direction.LOWER_IS_BETTER, tolerance=0.0), GateMetric("ROUTE_TOP1", Direction.HIGHER_IS_BETTER, tolerance=0.01, require_breakdowns=True), GateMetric("WER", Direction.LOWER_IS_BETTER, tolerance=0.005), GateMetric("INS_RATE", Direction.LOWER_IS_BETTER, tolerance=0.005))


@dataclass(slots=True)
class GateVerdict:
    passed: bool
    regressions: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return f"gate passed ({len(self.improvements)} improvements)"
        parts = []
        if self.regressions:
            parts.append(f"{len(self.regressions)} regressions")
        if self.missing:
            parts.append(f"{len(self.missing)} metrics missing")
        return "gate failed: " + ", ".join(parts)


def _regressed(value: float, baseline: float, metric: GateMetric) -> bool:
    if metric.direction is Direction.HIGHER_IS_BETTER:
        return value < baseline - metric.tolerance
    return value > baseline + metric.tolerance


def evaluate_gate(candidate: EvalRun, baseline: EvalRun | None, *, metrics: tuple[GateMetric, ...] = RELEASE_GATE_METRICS, require_all: bool = False) -> GateVerdict:
    """Compare a candidate run against the current production baseline."""
    verdict = GateVerdict(passed=True)

    if not candidate.is_release_gate:
        verdict.passed = False
        verdict.regressions.append(f"eval_run {candidate.id} is not marked is_release_gate; a smoke subset is for iteration, not for approving a release")
        return verdict

    if candidate.is_smoke_subset:
        verdict.passed = False
        verdict.regressions.append("a smoke-subset run cannot serve as a release gate; run the full set")
        return verdict

    candidate_metrics: dict[str, Any] = dict(candidate.metrics or {})
    baseline_metrics: dict[str, Any] = dict((baseline.metrics if baseline else {}) or {})

    for metric in metrics:
        value = candidate_metrics.get(metric.key)
        if value is None:
            verdict.missing.append(metric.key)
            if require_all:
                verdict.passed = False
            continue

        prior = baseline_metrics.get(metric.key)
        if prior is None:
            # No baseline for this metric: record the value, do not block. A
            # first run has nothing to regress against.
            continue

        if _regressed(float(value), float(prior), metric):
            verdict.passed = False
            verdict.regressions.append(f"{metric.key}: {float(value):.4f} vs baseline {float(prior):.4f} ({metric.direction})")
        else:
            verdict.improvements.append(metric.key)

        if metric.require_breakdowns:
            verdict.regressions.extend(_check_breakdowns(metric, candidate_metrics, baseline_metrics))
            verdict.passed = verdict.passed and not verdict.regressions

    return verdict


def _check_breakdowns(metric: GateMetric, candidate_metrics: dict[str, Any], baseline_metrics: dict[str, Any]) -> list[str]:
    """Per-group regression check ( "never averaged only")."""
    regressions: list[str] = []
    candidate_groups = candidate_metrics.get("_breakdowns") or {}
    baseline_groups = baseline_metrics.get("_breakdowns") or {}

    for dimension, groups in candidate_groups.items():
        baseline_dimension = baseline_groups.get(dimension) or {}
        for group, values in groups.items():
            value = values.get(metric.key)
            prior = (baseline_dimension.get(group) or {}).get(metric.key)
            if value is None or prior is None:
                continue
            if _regressed(float(value), float(prior), metric):
                regressions.append(f"{metric.key} regressed for {dimension}={group}: {float(value):.4f} vs {float(prior):.4f} (a change that helps most groups and breaks one must not pass on the average —)")
    return regressions
