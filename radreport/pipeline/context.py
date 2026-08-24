"""What a stage is handed while it runs. Stages never get a database session, so they cannot reach another lab's data.

Defines: RunContext, the object passed to every stage, and StageTiming, which records how long
each one took.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from radreport.core.errors import BudgetExceeded
from radreport.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from radreport.adapters.llm.registry import ResolvedModel, TaskModelResolver

log = get_logger(__name__)


@dataclass(slots=True)
class StageTiming:
    started_ns: int = field(default_factory=time.perf_counter_ns)

    def elapsed_ms(self) -> int:
        return (time.perf_counter_ns() - self.started_ns) // 1_000_000


@dataclass(slots=True)
class RunContext:
    """Concrete `StageContext`. One per `pipeline_run`."""

    tenant_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    recording_id: uuid.UUID
    is_shadow: bool = False

    budget_cap_usd: float | None = None
    spent_usd: float = 0.0

    resolver: TaskModelResolver | None = None
    template_id: uuid.UUID | None = None
    radiologist_id: uuid.UUID | None = None

    #: Per-stage spend, so the metering rollup and the optimisation
    #: table can both be computed from real numbers rather than estimates.
    cost_by_stage: dict[str, float] = field(default_factory=dict)

    _current_stage: str = "unknown"

    # ------------------------------------------------------------ costing ---
    def record_cost(self, usd: float, *, stage_name: str | None = None) -> None:
        """Accumulate spend and enforce the cap."""
        name = stage_name or self._current_stage
        self.spent_usd += usd
        self.cost_by_stage[name] = self.cost_by_stage.get(name, 0.0) + usd

        if self.budget_cap_usd is not None and self.spent_usd > self.budget_cap_usd:
            log.error("pipeline_budget_exceeded", pipeline_run_id=str(self.pipeline_run_id), stage=name, spent_usd=round(self.spent_usd, 4), cap_usd=self.budget_cap_usd)
            raise BudgetExceeded(self.spent_usd, self.budget_cap_usd)

    def enter_stage(self, stage_name: str) -> StageTiming:
        self._current_stage = stage_name
        return StageTiming()

    # ----------------------------------------------------------- resolving ---
    async def resolve_model(self, task_key: str) -> ResolvedModel:
        """ "Which model serves this task, for this tenant?"."""
        if self.resolver is None:
            raise RuntimeError("no TaskModelResolver bound to this run; every LLM call site must resolve through the registry rather than naming a model")
        return await self.resolver.resolve(task_key, tenant_id=self.tenant_id)

    # --------------------------------------------------------------- misc ---
    def as_log_context(self) -> dict[str, Any]:
        return {"tenant_id": str(self.tenant_id), "pipeline_run_id": str(self.pipeline_run_id), "recording_id": str(self.recording_id), "is_shadow": self.is_shadow}
