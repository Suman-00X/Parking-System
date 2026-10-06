"""Compares two time windows to show whether the system's behaviour has shifted, and where from.

Order: measure how far a numeric distribution moved (population_stability_index) or a
categorical one (categorical_drift) -> run every watched metric over both windows
(evaluate_drift) into a DriftReport.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.db.models.asr import Transcript
from radreport.db.models.ingestion import Recording
from radreport.db.models.reporting import ReportDraft, RoutingDecision
from radreport.db.models.review import ReportRevision

log = get_logger(__name__)

#: Conventional PSI bands.
PSI_MODERATE = 0.10
PSI_SIGNIFICANT = 0.25

#: Below this many observations a PSI is noise. Stated rather than left to the
#: reader: a PSI computed from 12 reports will be quoted later without its n.
MIN_SAMPLE = 50

#: Buckets for the [0, 1] metrics.
CONFIDENCE_BUCKETS: tuple[float, ...] = (0.0, 0.3, 0.5, 0.7, 0.85, 0.95, 1.01)


@dataclass(frozen=True, slots=True)
class DriftResult:
    metric: str
    psi: float | None
    baseline_n: int
    current_n: int
    baseline_distribution: dict[str, float] = field(default_factory=dict)
    current_distribution: dict[str, float] = field(default_factory=dict)

    @property
    def is_significant(self) -> bool:
        return self.psi is not None and self.psi >= PSI_SIGNIFICANT

    @property
    def is_moderate(self) -> bool:
        return self.psi is not None and PSI_MODERATE <= self.psi < PSI_SIGNIFICANT

    @property
    def band(self) -> str:
        if self.psi is None:
            return "insufficient_data"
        if self.psi >= PSI_SIGNIFICANT:
            return "significant"
        if self.psi >= PSI_MODERATE:
            return "moderate"
        return "stable"


def population_stability_index(baseline: list[float], current: list[float], *, buckets: tuple[float, ...] = CONFIDENCE_BUCKETS) -> DriftResult | None:
    """PSI between two samples of a bounded numeric metric."""
    if len(baseline) < MIN_SAMPLE or len(current) < MIN_SAMPLE:
        return None

    baseline_shares = _bucket_shares(baseline, buckets)
    current_shares = _bucket_shares(current, buckets)
    return DriftResult(metric="", psi=_psi(baseline_shares, current_shares), baseline_n=len(baseline), current_n=len(current), baseline_distribution=baseline_shares, current_distribution=current_shares)


def categorical_drift(baseline: list[str], current: list[str], *, metric: str = "") -> DriftResult | None:
    """PSI over a categorical distribution — template mix, engine mix, device class."""
    if len(baseline) < MIN_SAMPLE or len(current) < MIN_SAMPLE:
        return None

    categories = sorted(set(baseline) | set(current))
    baseline_counts, current_counts = Counter(baseline), Counter(current)
    baseline_shares = {c: baseline_counts[c] / len(baseline) for c in categories}
    current_shares = {c: current_counts[c] / len(current) for c in categories}

    return DriftResult(metric=metric, psi=_psi(baseline_shares, current_shares), baseline_n=len(baseline), current_n=len(current), baseline_distribution={k: round(v, 4) for k, v in baseline_shares.items()}, current_distribution={k: round(v, 4) for k, v in current_shares.items()})


def _bucket_shares(values: list[float], buckets: tuple[float, ...]) -> dict[str, float]:
    counts: Counter[str] = Counter()
    for value in values:
        for index in range(len(buckets) - 1):
            if buckets[index] <= value < buckets[index + 1]:
                counts[f"[{buckets[index]:.2f},{buckets[index + 1]:.2f})"] += 1
                break
    total = len(values) or 1
    return {f"[{buckets[i]:.2f},{buckets[i + 1]:.2f})": round(counts[f"[{buckets[i]:.2f},{buckets[i + 1]:.2f})"] / total, 4) for i in range(len(buckets) - 1)}


def _psi(baseline: dict[str, float], current: dict[str, float]) -> float:
    """Σ (c − b) · ln(c / b), with an epsilon floor on empty buckets."""
    epsilon = 1e-4
    total = 0.0
    for key in set(baseline) | set(current):
        b = max(baseline.get(key, 0.0), epsilon)
        c = max(current.get(key, 0.0), epsilon)
        total += (c - b) * math.log(c / b)
    return round(total, 4)


@dataclass(slots=True)
class DriftReport:
    tenant_id: uuid.UUID
    baseline_window: tuple[dt.datetime, dt.datetime]
    current_window: tuple[dt.datetime, dt.datetime]
    results: list[DriftResult] = field(default_factory=list)

    @property
    def significant(self) -> list[DriftResult]:
        return [r for r in self.results if r.is_significant]

    def as_json(self) -> dict[str, object]:
        return {"baseline_window": [d.isoformat() for d in self.baseline_window], "current_window": [d.isoformat() for d in self.current_window], "metrics": {r.metric: {"psi": r.psi, "band": r.band, "baseline_n": r.baseline_n, "current_n": r.current_n} for r in self.results}}


def evaluate_drift(session: Session, *, tenant_id: uuid.UUID, baseline_window: tuple[dt.datetime, dt.datetime], current_window: tuple[dt.datetime, dt.datetime]) -> DriftReport:
    """Compare two explicit windows across the metrics worth watching."""
    report = DriftReport(tenant_id=tenant_id, baseline_window=baseline_window, current_window=current_window)

    for metric, loader in (("draft_confidence", _draft_confidence), ("flagged_field_rate", _flagged_rate), ("asr_disagreement", _asr_disagreement), ("routing_confidence", _routing_confidence), ("active_edit_seconds", _active_edit_seconds)):
        baseline = loader(session, tenant_id, baseline_window)
        current = loader(session, tenant_id, current_window)
        result = population_stability_index(baseline, current)
        if result is not None:
            report.results.append(DriftResult(metric=metric, psi=result.psi, baseline_n=result.baseline_n, current_n=result.current_n, baseline_distribution=result.baseline_distribution, current_distribution=result.current_distribution))

    for metric, mix in (("template_mix", _template_mix), ("capture_device_class", _device_class_mix)):
        result = categorical_drift(mix(session, tenant_id, baseline_window), mix(session, tenant_id, current_window), metric=metric)
        if result is not None:
            report.results.append(result)

    if report.significant:
        # a change in behaviour is a change.
        log.warning("drift_detected", tenant_id=str(tenant_id), metrics=[r.metric for r in report.significant], psi={r.metric: r.psi for r in report.significant}, detail="treat as a pipeline change and re-run the gate")
    else:
        log.info("drift_evaluated", tenant_id=str(tenant_id), metrics_compared=len(report.results))
    return report


def _window(column, window: tuple[dt.datetime, dt.datetime]):
    return column.between(window[0], window[1])


def _draft_confidence(session: Session, tenant_id: uuid.UUID, window: tuple[dt.datetime, dt.datetime]) -> list[float]:
    rows = session.execute(select(ReportDraft.overall_confidence).where(ReportDraft.tenant_id == tenant_id, _window(ReportDraft.created_at, window))).scalars().all()
    return [float(v) for v in rows if v is not None]


def _flagged_rate(session: Session, tenant_id: uuid.UUID, window: tuple[dt.datetime, dt.datetime]) -> list[float]:
    """Flagged fields per draft, normalised into [0, 1]."""
    rows = session.execute(select(ReportDraft.flagged_field_count).where(ReportDraft.tenant_id == tenant_id, _window(ReportDraft.created_at, window))).scalars().all()
    return [min(1.0, (v or 0) / 20.0) for v in rows]


def _asr_disagreement(session: Session, tenant_id: uuid.UUID, window: tuple[dt.datetime, dt.datetime]) -> list[float]:
    rows = session.execute(select(Transcript.disagreement_score).where(Transcript.tenant_id == tenant_id, _window(Transcript.created_at, window))).scalars().all()
    return [float(v) for v in rows if v is not None]


def _routing_confidence(session: Session, tenant_id: uuid.UUID, window: tuple[dt.datetime, dt.datetime]) -> list[float]:
    rows = session.execute(select(RoutingDecision.confidence).where(RoutingDecision.tenant_id == tenant_id, _window(RoutingDecision.created_at, window))).scalars().all()
    return [float(v) for v in rows if v is not None]


def _active_edit_seconds(session: Session, tenant_id: uuid.UUID, window: tuple[dt.datetime, dt.datetime]) -> list[float]:
    """Normalised against a 10-minute ceiling, so the PSI buckets apply."""
    rows = session.execute(select(ReportRevision.active_edit_seconds).where(ReportRevision.tenant_id == tenant_id, _window(ReportRevision.created_at, window))).scalars().all()
    return [min(1.0, (v or 0) / 600.0) for v in rows]


def _template_mix(session: Session, tenant_id: uuid.UUID, window: tuple[dt.datetime, dt.datetime]) -> list[str]:
    rows = session.execute(select(ReportDraft.template_version_id).where(ReportDraft.tenant_id == tenant_id, _window(ReportDraft.created_at, window))).scalars().all()
    return [str(v) for v in rows]


def _device_class_mix(session: Session, tenant_id: uuid.UUID, window: tuple[dt.datetime, dt.datetime]) -> list[str]:
    rows = session.execute(select(Recording.capture_device_class).where(Recording.tenant_id == tenant_id, _window(Recording.uploaded_at, window))).scalars().all()
    return list(rows)
