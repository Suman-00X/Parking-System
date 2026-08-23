"""The gate on switching a step to a different model: it needs a passing gold-set evaluation, which no database constraint can express."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from radreport.adapters.llm.registry import activate_assignment
from radreport.core.errors import UngatedActivation
from radreport.core.types import AssignmentStatus, ProviderKind, TaskBucket, TaskKey


@dataclass
class FakeRow:
    values: tuple[Any, ...]

    def first(self) -> tuple[Any, ...]:
        return self.values

    def scalar_one_or_none(self) -> Any:
        return self.values[0] if self.values else None


@dataclass
class FakeSession:
    """Minimal stand-in: `get()` by id, `execute()` returning a queued result."""

    objects: dict[uuid.UUID, Any] = field(default_factory=dict)
    definition_row: tuple[Any, Any] | None = None
    active_by_task: dict[str, Any] = field(default_factory=dict)
    added: list[Any] = field(default_factory=list)

    def get(self, _model: Any, key: uuid.UUID) -> Any:
        return self.objects.get(key)

    def execute(self, statement: Any) -> Any:
        rendered = str(statement)
        if "model_definition" in rendered and "model_provider" in rendered:
            return FakeRow(self.definition_row or ())
        # The incumbent / independence lookups both filter on task_key; pull it
        # from the bound parameters.
        params = statement.compile().params
        task_key = next((v for k, v in params.items() if "task_key" in k and isinstance(v, str)), None)
        return FakeRow((self.active_by_task.get(task_key),))

    def flush(self) -> None:
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _assignment(*, task_key: str = TaskKey.EXTRACTION, bucket: str = TaskBucket.CONSEQUENTIAL, eval_run_id: uuid.UUID | None = None, tenant_id: uuid.UUID, model_definition_id: uuid.UUID | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, task_key=task_key, task_bucket=bucket, model_definition_id=model_definition_id or uuid.uuid4(), status=AssignmentStatus.PROPOSED, eval_run_id=eval_run_id, activated_at=None, activated_by=None, retired_at=None)


def _eval_run(*, gate: bool = True, task_key: str | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4(), is_release_gate=gate, task_key=task_key)


def _definition(*, kind: str = ProviderKind.CLOUD_API, identifier: str = "claude-sonnet-5") -> Any:
    from types import SimpleNamespace

    return (SimpleNamespace(id=uuid.uuid4(), model_identifier=identifier), SimpleNamespace(id=uuid.uuid4(), kind=kind, name="anthropic"))


def _session_for(assignment: Any, *, eval_run: Any | None = None, definition=None) -> FakeSession:
    objects: dict[uuid.UUID, Any] = {assignment.id: assignment}
    if eval_run is not None:
        objects[eval_run.id] = eval_run
    return FakeSession(objects=objects, definition_row=definition or _definition())


def test_activation_without_an_eval_run_is_refused() -> None:
    """The headline rule. Without it, the model-config surface becomes a way to change a clinical pipeline with no evidence behind it."""
    tenant = uuid.uuid4()
    assignment = _assignment(tenant_id=tenant, eval_run_id=None)
    session = _session_for(assignment)

    with pytest.raises(UngatedActivation, match="gold-set eval_run"):
        activate_assignment(session, assignment_id=assignment.id, tenant_id=tenant, actor_id=uuid.uuid4())
    assert assignment.status == AssignmentStatus.PROPOSED


def test_a_non_gate_eval_run_cannot_approve_an_activation() -> None:
    """The smoke subset is for iteration, not for approving a swap."""
    tenant = uuid.uuid4()
    run = _eval_run(gate=False)
    assignment = _assignment(tenant_id=tenant, eval_run_id=run.id)

    with pytest.raises(UngatedActivation, match="not a release gate"):
        activate_assignment(_session_for(assignment, eval_run=run), assignment_id=assignment.id, tenant_id=tenant, actor_id=uuid.uuid4())


def test_an_eval_run_for_a_different_task_cannot_approve_this_one() -> None:
    """The per-task swap cuts both ways: a run scoped to `routing_shortlist` says nothing about extraction."""
    tenant = uuid.uuid4()
    run = _eval_run(task_key=TaskKey.ROUTING_SHORTLIST)
    assignment = _assignment(tenant_id=tenant, task_key=TaskKey.EXTRACTION, eval_run_id=run.id)

    with pytest.raises(UngatedActivation, match="scoped to task"):
        activate_assignment(_session_for(assignment, eval_run=run), assignment_id=assignment.id, tenant_id=tenant, actor_id=uuid.uuid4())


@pytest.mark.parametrize("task_key", [TaskKey.EXTRACTION, TaskKey.SELF_CORRECTION, TaskKey.VERIFICATION])
def test_consequential_tasks_cannot_route_to_a_local_model(task_key: str) -> None:
    """The scope discipline: extraction, self-correction and verification stay on a frontier model **regardless of local hardware capability**."""
    tenant = uuid.uuid4()
    run = _eval_run()
    assignment = _assignment(tenant_id=tenant, task_key=task_key, eval_run_id=run.id)
    session = _session_for(assignment, eval_run=run, definition=_definition(kind=ProviderKind.LOCAL_OPENAI_COMPATIBLE, identifier="llama-3.1-8b"))

    with pytest.raises(UngatedActivation, match="consequential"):
        activate_assignment(session, assignment_id=assignment.id, tenant_id=tenant, actor_id=uuid.uuid4())


def test_compose_and_roundtrip_check_may_not_share_a_model() -> None:
    """Plan caution 1."""
    tenant = uuid.uuid4()
    shared_model = uuid.uuid4()
    run = _eval_run()

    incumbent_roundtrip = _assignment(tenant_id=tenant, task_key=TaskKey.ROUNDTRIP_CHECK, bucket=TaskBucket.BOUNDED, model_definition_id=shared_model)
    incumbent_roundtrip.status = AssignmentStatus.ACTIVE

    assignment = _assignment(tenant_id=tenant, task_key=TaskKey.COMPOSE, eval_run_id=run.id, model_definition_id=shared_model)
    session = _session_for(assignment, eval_run=run)
    session.active_by_task[TaskKey.ROUNDTRIP_CHECK] = incumbent_roundtrip

    with pytest.raises(UngatedActivation, match="independence"):
        activate_assignment(session, assignment_id=assignment.id, tenant_id=tenant, actor_id=uuid.uuid4())


def test_a_properly_gated_activation_succeeds_and_retires_the_incumbent() -> None:
    """The partial unique index allows exactly one active row per (tenant, task_key), so activating must retire the incumbent in the same transaction."""
    tenant = uuid.uuid4()
    actor = uuid.uuid4()
    run = _eval_run(task_key=TaskKey.EXTRACTION)

    incumbent = _assignment(tenant_id=tenant, task_key=TaskKey.EXTRACTION)
    incumbent.status = AssignmentStatus.ACTIVE

    assignment = _assignment(tenant_id=tenant, task_key=TaskKey.EXTRACTION, eval_run_id=run.id)
    session = _session_for(assignment, eval_run=run)
    session.active_by_task[TaskKey.EXTRACTION] = incumbent

    activated = activate_assignment(session, assignment_id=assignment.id, tenant_id=tenant, actor_id=actor)

    assert activated.status == AssignmentStatus.ACTIVE
    assert activated.activated_by == actor
    assert incumbent.status == AssignmentStatus.RETIRED
    assert incumbent.retired_at is not None
    # Both events land in the append-only log.
    assert len(session.added) == 2


def test_activating_another_tenants_assignment_is_refused() -> None:
    from radreport.core.errors import ModelResolutionError

    assignment = _assignment(tenant_id=uuid.uuid4(), eval_run_id=uuid.uuid4())
    with pytest.raises(ModelResolutionError):
        activate_assignment(
            _session_for(assignment),
            assignment_id=assignment.id,
            tenant_id=uuid.uuid4(),  # a different tenant
            actor_id=uuid.uuid4(),
        )
