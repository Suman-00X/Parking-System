"""Runs a sequence of stages in order and records what happened. This file holds the machinery; v1.py holds the actual order.

Order: describe each stage and its wiring (StageSpec) -> assemble them (PipelineGraph) ->
start a run (new_run).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from radreport.core.errors import BudgetExceeded, StageFailed
from radreport.core.logging import get_logger
from radreport.core.types import RunStatus
from radreport.db.models.orchestration import PipelineRun, StageExecution
from radreport.observability.metrics import observe_stage
from radreport.observability.tracing import span
from radreport.pipeline.context import RunContext
from radreport.pipeline.contracts import Stage, StageResult
from radreport.pipeline.state import PipelineState

log = get_logger(__name__)

#: Bumped whenever the stage list or any stage version changes.
PIPELINE_VERSION = "0.1.0-phase0"

#: caps the critic loop. Enforced here, not in the stage: a stage must not
#: be able to grant itself another iteration.
MAX_CRITIC_ITERATIONS = 3


@dataclass(slots=True)
class StageSpec:
    """One node. `writes_domain_rows` is documentation *and* a shadow-mode switch."""

    stage: Stage
    task_key: str | None = None
    """Which task this stage invokes, if any. Recorded on `stage_execution` so per-task cost and per-task eval line up."""

    optional: bool = False
    """A failure here degrades the run instead of failing it. Never true for critical-findings detection or grounding."""


class PipelineGraph:
    """The ordered stage list plus the orchestration rules around it."""

    def __init__(self, specs: Sequence[StageSpec], *, version: str = PIPELINE_VERSION) -> None:
        self.specs = list(specs)
        self.version = version
        names = [s.stage.name for s in self.specs]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate stage names in graph: {names}")

    def stage_names(self) -> tuple[str, ...]:
        return tuple(s.stage.name for s in self.specs)

    async def run(self, state: PipelineState, ctx: RunContext, session: Session) -> PipelineState:
        """Execute the graph, committing orchestration rows as we go."""
        run = session.get(PipelineRun, ctx.pipeline_run_id)
        if run is None:
            raise StageFailed("graph", f"no pipeline_run {ctx.pipeline_run_id}")

        run.status = RunStatus.RUNNING
        run.started_at = dt.datetime.now(dt.UTC)
        session.flush()

        # Stage rows and domain writes are buffered and flushed once at the end, so each table gets one batched INSERT.
        # Nothing reads them mid-run: stages never touch the session, and every id is assigned before the row is added.
        buffer: list[object] = []
        try:
            for spec in self.specs:
                state = await self._run_one(spec, state, ctx, buffer)
        except BudgetExceeded as exc:
            self._finish(session, run, ctx, RunStatus.FAILED, {"budget": str(exc)}, buffer)
            raise
        except StageFailed as exc:
            self._finish(session, run, ctx, RunStatus.FAILED, {"stage": exc.stage_name, "detail": exc.detail}, buffer)
            raise

        self._finish(session, run, ctx, RunStatus.SUCCEEDED, None, buffer)
        state.total_cost_usd = ctx.spent_usd
        return state

    # ------------------------------------------------------------ internals --
    async def _run_one(self, spec: StageSpec, state: PipelineState, ctx: RunContext, buffer: list[object]) -> PipelineState:
        stage = spec.stage
        timing = ctx.enter_stage(stage.name)

        execution = StageExecution(
            # Both set here rather than by the server, so the batched INSERT needs no RETURNING; created_at is then the stage's real start.
            id=uuid.uuid4(),
            created_at=dt.datetime.now(dt.UTC),
            tenant_id=ctx.tenant_id,
            pipeline_run_id=ctx.pipeline_run_id,
            stage_name=stage.name,
            attempt=1,
            # Pointers, not payloads. Keeping transcripts out of the
            # orchestration tables is what makes replay cheap.
            input_ref={"recording_id": str(ctx.recording_id), "stage_version": stage.version},
            task_key=spec.task_key,
            template_id=ctx.template_id,
            radiologist_id=ctx.radiologist_id,
            status=RunStatus.RUNNING,
        )
        buffer.append(execution)

        try:
            with span(f"stage {stage.name}", stage=stage.name, task_key=spec.task_key, tenant_id=str(ctx.tenant_id)):
                result: StageResult = await stage.run(state, ctx)
        except Exception as exc:
            execution.status = RunStatus.FAILED
            execution.duration_ms = timing.elapsed_ms()
            observe_stage(stage.name, "failed", execution.duration_ms)
            execution.output_ref = {"error": type(exc).__name__, "detail": str(exc)[:500]}
            if spec.optional:
                log.warning("optional_stage_failed", stage=stage.name, error=str(exc)[:200], **ctx.as_log_context())
                return state
            raise StageFailed(stage.name, str(exc)) from exc

        # Cost first: a stage that called a provider has already spent the
        # money, so record it even if the cap then aborts the run.
        if result.cost_usd:
            ctx.record_cost(result.cost_usd, stage_name=stage.name)

        execution.status = RunStatus.SUCCEEDED
        execution.duration_ms = result.duration_ms or timing.elapsed_ms()
        execution.model_id = result.model_id
        execution.model_version = result.model_version
        execution.prompt_version = result.prompt_version
        execution.tokens_in = result.tokens_in
        execution.tokens_out = result.tokens_out
        execution.cache_read_tokens = result.cache_read_tokens
        execution.cache_write_tokens = result.cache_write_tokens
        execution.cost_usd = result.cost_usd
        observe_stage(stage.name, "succeeded", execution.duration_ms, tokens_in=result.tokens_in, tokens_out=result.tokens_out, cache_read_tokens=result.cache_read_tokens, cache_write_tokens=result.cache_write_tokens, cost_usd=result.cost_usd)
        execution.output_ref = {"warnings": result.warnings} if result.warnings else {}

        # Domain writes belong to the orchestrator, never to the stage.
        if result.pending_writes:
            if ctx.is_shadow:
                log.info("shadow_writes_discarded", stage=stage.name, count=len(result.pending_writes), **ctx.as_log_context())
            else:
                buffer.extend(result.pending_writes)

        return result.output if isinstance(result.output, PipelineState) else state

    def _finish(self, session: Session, run: PipelineRun, ctx: RunContext, status: str, error: dict[str, object] | None, buffer: list[object]) -> None:
        session.add_all(buffer)
        run.status = status
        run.completed_at = dt.datetime.now(dt.UTC)
        run.total_cost_usd = round(ctx.spent_usd, 4)
        if error is not None:
            run.error_detail = error
        session.flush()


def new_run(session: Session, *, tenant_id: uuid.UUID, recording_id: uuid.UUID, trigger: str, is_shadow: bool = False, budget_cap_usd: float | None = None, pipeline_version: str = PIPELINE_VERSION) -> tuple[PipelineRun, RunContext, PipelineState]:
    """Create the `pipeline_run` row and its context/state pair."""
    run = PipelineRun(tenant_id=tenant_id, recording_id=recording_id, pipeline_version=pipeline_version, trigger=trigger, is_shadow=is_shadow, status=RunStatus.QUEUED, budget_cap_usd=budget_cap_usd)
    session.add(run)
    session.flush()

    ctx = RunContext(tenant_id=tenant_id, pipeline_run_id=run.id, recording_id=recording_id, is_shadow=is_shadow, budget_cap_usd=budget_cap_usd)
    state = PipelineState(tenant_id=tenant_id, recording_id=recording_id, pipeline_run_id=run.id, is_shadow=is_shadow)
    return run, ctx, state
