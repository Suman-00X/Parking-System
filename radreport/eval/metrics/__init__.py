"""The metric registry: each metric declares which stage owns it and how it is computed.

Order: Metric describes one measure, MetricRegistry holds them, and default_registry returns the
standard set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from radreport.db.models.evaluation import EvalItem
    from radreport.eval.harness import StageOutputs


class Metric(Protocol):
    key: str
    owner_stage: str
    task_keys: tuple[str, ...]
    """Which `task_key`-scoped runs compute this metric."""

    def score(self, item: EvalItem, stage_outputs: dict[str, StageOutputs]) -> tuple[float | None, dict[str, object] | None]:
        """Returns (value, error_detail). `None` means not applicable."""
        ...


@dataclass(slots=True)
class MetricRegistry:
    _metrics: list[Metric]

    def metrics(self, *, task_key: str | None = None) -> list[Metric]:
        """Metrics applicable to a run."""
        if task_key is None:
            return list(self._metrics)
        return [m for m in self._metrics if task_key in m.task_keys]

    def register(self, metric: Metric) -> None:
        if any(m.key == metric.key for m in self._metrics):
            raise ValueError(f"metric {metric.key!r} is already registered")
        self._metrics.append(metric)

    def keys(self) -> tuple[str, ...]:
        return tuple(m.key for m in self._metrics)


def default_registry() -> MetricRegistry:
    from radreport.eval.metrics.asr_metrics import ClinicalTermErrorRate, InsertionRate, WordErrorRate
    from radreport.eval.metrics.routing_metrics import CodewordCompliance, RouteTop1, StudyCodeRecall

    return MetricRegistry([WordErrorRate(), InsertionRate(), ClinicalTermErrorRate(), CodewordCompliance(), StudyCodeRecall(), RouteTop1()])
