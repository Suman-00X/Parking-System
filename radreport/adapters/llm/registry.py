"""Decides which model serves a given pipeline step for a given lab, and refuses an ungated switch.

Order: resolve the model for a step (TaskModelResolver) -> make a proposed change live, but only
with a passing gold-set evaluation behind it (activate_assignment).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.adapters.llm.base import ResolvedModelRef
from radreport.adapters.llm.pricing import derive_cache_prices
from radreport.cache.keys import key as cache_key
from radreport.cache.request import forget, request_cached
from radreport.core.errors import ModelResolutionError, UngatedActivation
from radreport.core.logging import get_logger
from radreport.core.types import CONSEQUENTIAL_TASKS, AssignmentEvent, AssignmentStatus, ProviderKind, TaskKey
from radreport.db.models.evaluation import EvalRun
from radreport.db.models.modelconfig import ModelDefinition, ModelProvider, TaskModelAssignment, TaskModelAssignmentLog

log = get_logger(__name__)

#: caution 1. Local compose is a Beta move, and only with the round-trip check kept on a *different* (cloud) model.
INDEPENDENCE_PAIRS: tuple[tuple[str, str], ...] = ((TaskKey.COMPOSE, TaskKey.ROUNDTRIP_CHECK),)


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """What a call site gets back."""

    ref: ResolvedModelRef
    task_key: str
    task_bucket: str
    assignment_id: uuid.UUID
    eval_run_id: uuid.UUID | None


class TaskModelResolver:
    """Reads the live assignment for (tenant, task)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._cache: dict[tuple[uuid.UUID, str], ResolvedModel] = {}

    async def resolve(self, task_key: str, *, tenant_id: uuid.UUID) -> ResolvedModel:
        key = (tenant_id, task_key)
        if key in self._cache:
            return self._cache[key]

        resolved = request_cached(cache_key("model_assignment", tenant_id, task_key), lambda: self._load(task_key, tenant_id))
        self._cache[key] = resolved
        return resolved

    def _load(self, task_key: str, tenant_id: uuid.UUID) -> ResolvedModel:
        row = self._session.execute(select(TaskModelAssignment, ModelDefinition, ModelProvider).join(ModelDefinition, ModelDefinition.id == TaskModelAssignment.model_definition_id).join(ModelProvider, ModelProvider.id == ModelDefinition.provider_id).where(TaskModelAssignment.tenant_id == tenant_id, TaskModelAssignment.task_key == task_key, TaskModelAssignment.status == AssignmentStatus.ACTIVE)).first()

        if row is None:
            raise ModelResolutionError(f"no active model assignment for task {task_key!r} in tenant {tenant_id}")

        assignment, definition, provider = row
        return ResolvedModel(ref=_to_ref(definition, provider), task_key=task_key, task_bucket=assignment.task_bucket, assignment_id=assignment.id, eval_run_id=assignment.eval_run_id)

    def invalidate(self, tenant_id: uuid.UUID | None = None) -> None:
        if tenant_id is None:
            self._cache.clear()
        else:
            for key in [k for k in self._cache if k[0] == tenant_id]:
                del self._cache[key]


def _to_ref(definition: ModelDefinition, provider: ModelProvider) -> ResolvedModelRef:
    input_price = float(definition.input_price_per_1k or 0.0)
    output_price = float(definition.output_price_per_1k or 0.0)

    read_price = definition.cache_read_price_per_1k
    write_price = definition.cache_write_price_per_1k
    if read_price is None or write_price is None:
        derived_read, derived_write = derive_cache_prices(input_price)
        read_price = read_price if read_price is not None else derived_read
        write_price = write_price if write_price is not None else derived_write

    return ResolvedModelRef(model_definition_id=definition.id, model_identifier=definition.model_identifier, provider_name=provider.name, provider_kind=provider.kind, endpoint=definition.endpoint_override or provider.default_endpoint, input_price_per_1k=input_price, output_price_per_1k=output_price, cache_read_price_per_1k=float(read_price), cache_write_price_per_1k=float(write_price), batch_discount_factor=float(definition.batch_discount_factor or 1.0), context_window=definition.context_window)


# --------------------------------------------------------------- activation --
def activate_assignment(session: Session, *, assignment_id: uuid.UUID, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TaskModelAssignment:
    """Promote a proposed assignment to `active`, through the gate."""
    assignment = session.get(TaskModelAssignment, assignment_id)
    if assignment is None or assignment.tenant_id != tenant_id:
        raise ModelResolutionError(f"no assignment {assignment_id} in tenant {tenant_id}")

    if assignment.eval_run_id is None:
        raise UngatedActivation(f"task {assignment.task_key!r} cannot go active without a gold-set eval_run: the cost table is a hypothesis; only the eval says whether accuracy survives")

    eval_run = session.get(EvalRun, assignment.eval_run_id)
    if eval_run is None:
        raise UngatedActivation(f"eval_run {assignment.eval_run_id} does not exist")
    if not eval_run.is_release_gate:
        raise UngatedActivation(f"eval_run {eval_run.id} is not a release gate; a smoke-subset run is for iteration, not for approving a swap")
    if eval_run.task_key is not None and eval_run.task_key != assignment.task_key:
        raise UngatedActivation(f"eval_run scoped to task {eval_run.task_key!r} cannot approve task {assignment.task_key!r}")

    definition, provider = _definition_and_provider(session, assignment.model_definition_id)

    if assignment.task_key in CONSEQUENTIAL_TASKS and provider.kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE:
        raise UngatedActivation(f"task {assignment.task_key!r} is consequential; extraction, self-correction and verification stay on a frontier model regardless of local hardware capability")

    _assert_independence(session, assignment, tenant_id)

    incumbent = session.execute(select(TaskModelAssignment).where(TaskModelAssignment.tenant_id == tenant_id, TaskModelAssignment.task_key == assignment.task_key, TaskModelAssignment.status == AssignmentStatus.ACTIVE)).scalar_one_or_none()

    now = dt.datetime.now(dt.UTC)
    if incumbent is not None and incumbent.id != assignment.id:
        incumbent.status = AssignmentStatus.RETIRED
        incumbent.retired_at = now
        session.add(TaskModelAssignmentLog(tenant_id=tenant_id, task_key=incumbent.task_key, model_definition_id=incumbent.model_definition_id, event=AssignmentEvent.RETIRED, actor_id=actor_id))
        session.flush()

    assignment.status = AssignmentStatus.ACTIVE
    forget(cache_key("model_assignment", tenant_id, assignment.task_key))
    assignment.activated_at = now
    assignment.activated_by = actor_id
    session.add(TaskModelAssignmentLog(tenant_id=tenant_id, task_key=assignment.task_key, model_definition_id=assignment.model_definition_id, event=AssignmentEvent.ACTIVATED, eval_run_id=assignment.eval_run_id, actor_id=actor_id))
    session.flush()

    log.info("model_assignment_activated", tenant_id=str(tenant_id), task_key=assignment.task_key, model_identifier=definition.model_identifier, eval_run_id=str(assignment.eval_run_id))
    return assignment


def _definition_and_provider(session: Session, model_definition_id: uuid.UUID) -> tuple[ModelDefinition, ModelProvider]:
    row = session.execute(select(ModelDefinition, ModelProvider).join(ModelProvider, ModelProvider.id == ModelDefinition.provider_id).where(ModelDefinition.id == model_definition_id)).first()
    if row is None:
        raise ModelResolutionError(f"no model_definition {model_definition_id}")
    return row[0], row[1]


def _assert_independence(session: Session, assignment: TaskModelAssignment, tenant_id: uuid.UUID) -> None:
    """Keep paired tasks on different models ( caution 1)."""
    for a, b in INDEPENDENCE_PAIRS:
        if assignment.task_key not in (a, b):
            continue
        other_key = b if assignment.task_key == a else a
        other = session.execute(select(TaskModelAssignment).where(TaskModelAssignment.tenant_id == tenant_id, TaskModelAssignment.task_key == other_key, TaskModelAssignment.status == AssignmentStatus.ACTIVE)).scalar_one_or_none()
        if other is not None and other.model_definition_id == assignment.model_definition_id:
            raise UngatedActivation(f"{a!r} and {b!r} would share a model. The round-trip entailment is what catches a compose that drops or garbles a grounded atom; a model checking its own work removes the independence the check depends on")
