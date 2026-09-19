"""Runs the pipeline over a gold set and collects each stage's output for scoring.

Order: EvalHarness runs the pipeline in an EvalContext, gather_stage collects each stage's
output (StageOutputs), and assert_no_eval_leakage refuses a run whose audio was used for
training.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.errors import EvalSetLeakage
from radreport.core.logging import get_logger
from radreport.core.types import CaptureDeviceClass
from radreport.db.models.evaluation import EvalItem, EvalResult, EvalRun, EvalSet
from radreport.eval.metrics import MetricRegistry, default_registry

log = get_logger(__name__)

#: fix 2: a small subset for iteration, the full set only on release
#: candidates. Cuts the feedback loop from ₹630 to ~₹126 a run.
SMOKE_SUBSET_SIZE = 30


@dataclass(slots=True)
class EvalContext:
    """What one item's evaluation gets."""

    item: EvalItem
    run_id: uuid.UUID
    task_key: str | None


@dataclass(slots=True)
class StageOutputs:
    """Per-item outputs from one stage, keyed by item id."""

    stage_name: str
    outputs: dict[uuid.UUID, object] = field(default_factory=dict)
    failures: dict[uuid.UUID, str] = field(default_factory=dict)


StageRunner = Callable[[Sequence[EvalItem]], Awaitable[StageOutputs]]
"""A stage, applied across **all** items at once."""


class EvalHarness:
    """Runs a stage-synchronised evaluation over a gold set."""

    def __init__(self, session: Session, *, registry: MetricRegistry | None = None) -> None:
        self._session = session
        self._registry = registry or default_registry()

    # ------------------------------------------------------------ selection --
    def load_items(self, eval_set_id: uuid.UUID, *, device_class: str | None = CaptureDeviceClass.DICTATION_MIC_PTT, smoke: bool = False, limit: int | None = None) -> list[EvalItem]:
        """Load gold items, **partitioned by capture hardware**."""
        query = select(EvalItem).where(EvalItem.eval_set_id == eval_set_id)
        if device_class is not None:
            query = query.where(EvalItem.capture_device_class == device_class)
        query = query.order_by(EvalItem.id)

        items = list(self._session.execute(query).scalars().all())
        if smoke:
            items = _stratified_sample(items, SMOKE_SUBSET_SIZE)
        if limit is not None:
            items = items[:limit]
        return items

    # ----------------------------------------------------------- execution --
    async def run(self, *, eval_set_id: uuid.UUID, pipeline_version: str, stages: Sequence[tuple[str, StageRunner]], task_key: str | None = None, is_release_gate: bool = False, smoke: bool = False, used_batch_api: bool = False, device_class: str | None = CaptureDeviceClass.DICTATION_MIC_PTT, config_snapshot: dict[str, object] | None = None) -> EvalRun:
        """Execute `stages` in order, each across all items, then score."""
        eval_set = self._session.get(EvalSet, eval_set_id)
        if eval_set is None:
            raise ValueError(f"no eval_set {eval_set_id}")
        if is_release_gate and not eval_set.is_frozen:
            raise ValueError(f"eval_set {eval_set.name!r} is not frozen; a release gate run against a mutable set proves nothing")

        items = self.load_items(eval_set_id, device_class=device_class, smoke=smoke)
        if not items:
            raise ValueError(f"eval_set {eval_set_id} yielded no items for device_class={device_class}")

        run = EvalRun(tenant_id=eval_set.tenant_id, eval_set_id=eval_set_id, pipeline_version=pipeline_version, task_key=task_key, config_snapshot=config_snapshot or {}, is_smoke_subset=smoke, used_batch_api=used_batch_api, is_release_gate=is_release_gate, started_at=dt.datetime.now(dt.UTC))
        self._session.add(run)
        self._session.flush()

        stage_outputs: dict[str, StageOutputs] = {}
        for stage_name, runner in stages:
            outputs = await runner(items)
            stage_outputs[stage_name] = outputs
            log.info("eval_stage_complete", run_id=str(run.id), stage=stage_name, ok=len(outputs.outputs), failed=len(outputs.failures))

        metrics = self._score(run, items, stage_outputs)
        run.metrics = metrics
        run.completed_at = dt.datetime.now(dt.UTC)
        self._session.flush()
        return run

    # -------------------------------------------------------------- scoring --
    def _score(self, run: EvalRun, items: Sequence[EvalItem], stage_outputs: dict[str, StageOutputs]) -> dict[str, object]:
        per_item: list[EvalResult] = []
        aggregates: dict[str, list[float]] = {}

        for item in items:
            for metric in self._registry.metrics(task_key=run.task_key):
                value, detail = metric.score(item, stage_outputs)
                if value is None:
                    continue
                per_item.append(EvalResult(tenant_id=run.tenant_id, eval_run_id=run.id, eval_item_id=item.id, metric_key=metric.key, metric_value=value, error_detail=detail))
                aggregates.setdefault(metric.key, []).append(value)

        self._session.add_all(per_item)
        self._session.flush()

        summary: dict[str, object] = {key: round(sum(values) / len(values), 6) for key, values in aggregates.items()}
        # ROUTE_TOP1 is "reported per template, never averaged only" extends that to per source tenant — a model change that helps eight labs and breaks the ninth must be visible as such.
        summary["_breakdowns"] = {"by_source_tenant": _breakdown(items, per_item, lambda i: str(i.source_tenant_id)), "by_device_class": _breakdown(items, per_item, lambda i: i.capture_device_class), "by_audio_quality": _breakdown(items, per_item, lambda i: i.audio_quality_bucket or "unknown")}
        summary["_item_count"] = len(items)
        return summary


def _breakdown(items: Sequence[EvalItem], results: Sequence[EvalResult], key_fn: Callable[[EvalItem], str]) -> dict[str, dict[str, float]]:
    item_key = {item.id: key_fn(item) for item in items}
    buckets: dict[str, dict[str, list[float]]] = {}
    for result in results:
        if result.metric_value is None:
            continue
        group = item_key.get(result.eval_item_id, "unknown")
        buckets.setdefault(group, {}).setdefault(result.metric_key, []).append(float(result.metric_value))
    return {group: {metric: round(sum(v) / len(v), 6) for metric, v in metrics.items()} for group, metrics in buckets.items()}


def _stratified_sample(items: list[EvalItem], size: int) -> list[EvalItem]:
    """Sample proportionally across quality buckets."""
    if len(items) <= size:
        return items
    strata: dict[str, list[EvalItem]] = {}
    for item in items:
        strata.setdefault(item.audio_quality_bucket or "unknown", []).append(item)

    picked: list[EvalItem] = []
    for bucket_items in strata.values():
        share = max(1, round(size * len(bucket_items) / len(items)))
        picked.extend(bucket_items[:share])
    return picked[:size]


def assert_no_eval_leakage(session: Session, *, snapshot_item_ids: Sequence[uuid.UUID]) -> None:
    """Guard training snapshots against gold-set contamination."""
    if not snapshot_item_ids:
        return
    overlapping = session.execute(select(EvalItem.recording_id).where(EvalItem.recording_id.in_(snapshot_item_ids))).scalars().all()
    if overlapping:
        raise EvalSetLeakage(f"{len(overlapping)} recording(s) are in both the training snapshot and an eval set: {[str(r) for r in overlapping[:5]]}")


async def gather_stage(items: Sequence[EvalItem], stage_name: str, fn: Callable[[EvalItem], Awaitable[object]], *, max_concurrency: int = 8) -> StageOutputs:
    """Live (non-batched) fan-out of one stage across items."""
    semaphore = asyncio.Semaphore(max_concurrency)
    result = StageOutputs(stage_name=stage_name)

    async def _one(item: EvalItem) -> None:
        async with semaphore:
            try:
                result.outputs[item.id] = await fn(item)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                result.failures[item.id] = f"{type(exc).__name__}: {exc}"

    await asyncio.gather(*(_one(item) for item in items))
    return result
