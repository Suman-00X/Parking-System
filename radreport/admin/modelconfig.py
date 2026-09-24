"""Configures which language model serves which pipeline step, per lab.

Order: register a vendor (create_provider, resolve_api_key) -> register a model
(create_definition) -> propose it for a step (task_bucket_for, propose_assignment) -> read the
current picture (step_configuration, available_models, unconfigured_steps).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import ASR_TASKS, CONSEQUENTIAL_TASKS, ActorType, AssignmentEvent, AssignmentStatus, AuthMethod, ProviderKind, TaskBucket, TaskKey
from radreport.db.models.modelconfig import ModelDefinition, ModelProvider, TaskModelAssignment, TaskModelAssignmentLog
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.tenancy import Tenant
from radreport.db.session import bind_tenant

log = get_logger(__name__)


class ConfigRefused(Exception):
    """A configuration change that would produce an unusable or unsafe setup."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


# =============================================================== providers ===
def create_provider(session: Session, *, name: str, kind: str, actor_id: uuid.UUID, default_endpoint: str | None = None, api_key_env_var: str | None = None, auth_method: str = AuthMethod.API_KEY, tenant_id: uuid.UUID | None = None) -> ModelProvider:
    """Register a provider. `tenant_id=None` is the global catalog."""
    if kind not in ProviderKind.values():
        raise ConfigRefused(f"unknown provider kind {kind!r}", code="bad_kind")
    if auth_method not in AuthMethod.values():
        raise ConfigRefused(f"unknown auth method {auth_method!r}", code="bad_auth")

    if kind == ProviderKind.CLOUD_API and auth_method == AuthMethod.API_KEY:
        if not api_key_env_var:
            raise ConfigRefused("a cloud provider must name the environment variable holding its API key (the key itself is never stored)", code="no_env_var")
        if not os.environ.get(api_key_env_var):
            raise ConfigRefused(f"{api_key_env_var} is not set in this environment; the provider would be unusable at the first call", code="env_var_unset")

    if kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE and not api_key_env_var:
        # A local box usually needs no credential, and `auth_method` defaults to `api_key` at the column.
        auth_method = AuthMethod.NONE

    if kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE and not default_endpoint:
        raise ConfigRefused("a locally hosted provider needs an endpoint; there is no default address for a box only this lab can reach", code="no_endpoint")

    existing = session.execute(select(ModelProvider).where(ModelProvider.tenant_id == tenant_id, ModelProvider.name == name)).scalar_one_or_none()
    if existing is not None:
        raise ConfigRefused(f"a provider named {name!r} already exists", code="duplicate")

    if tenant_id is not None:
        # RLS refuses a tenant-owned row from a tenantless session — correctly: the admin panel runs as a product admin who belongs to no lab, so it must narrow onto the lab it is writing for (as `register_lab` does).
        bind_tenant(session, tenant_id)

    provider = ModelProvider(tenant_id=tenant_id, name=name, kind=kind, default_endpoint=default_endpoint, auth_method=auth_method, api_key_env_var=api_key_env_var)
    session.add(provider)
    session.flush()

    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_type=ActorType.USER,
            action="model_provider_created",
            entity_type="model_provider",
            entity_id=provider.id,
            after={
                "name": name,
                "kind": kind,
                # The variable's *name* is safe to log; its value is not, and
                # is never read here.
                "api_key_env_var": api_key_env_var,
                "default_endpoint": default_endpoint,
            },
        )
    )
    session.flush()
    log.info("model_provider_created", name=name, kind=kind, tenant_id=str(tenant_id))
    return provider


def resolve_api_key(provider: ModelProvider) -> str | None:
    """The provider's key, from the environment. Never from the database."""
    if provider.auth_method == AuthMethod.NONE:
        return None
    if not provider.api_key_env_var:
        raise ConfigRefused(f"provider {provider.name!r} expects an API key but names no environment variable to read it from", code="no_env_var")
    value = os.environ.get(provider.api_key_env_var)
    if not value:
        raise ConfigRefused(f"{provider.api_key_env_var} is not set; provider {provider.name!r} cannot authenticate", code="env_var_unset")
    return value


# ============================================================= definitions ===
def create_definition(session: Session, *, provider_id: uuid.UUID, model_identifier: str, display_name: str, actor_id: uuid.UUID, tenant_id: uuid.UUID | None = None, endpoint_override: str | None = None, input_price_per_1k: float | None = None, output_price_per_1k: float | None = None, cache_read_price_per_1k: float | None = None, cache_write_price_per_1k: float | None = None, context_window: int | None = None) -> ModelDefinition:
    """Add a model to the catalog, or to one lab's catalog."""
    provider = session.get(ModelProvider, provider_id)
    if provider is None:
        raise ConfigRefused(f"no model_provider {provider_id}", code="no_provider")
    if provider.tenant_id is not None and provider.tenant_id != tenant_id:
        raise ConfigRefused("a model cannot be defined against another lab's private provider", code="cross_tenant_provider")
    if "-20" in model_identifier and model_identifier.rstrip("0123456789")[-1:] == "-":
        raise ConfigRefused(f"{model_identifier!r} looks date-suffixed; the API rejects those. Use the bare identifier, e.g. 'claude-sonnet-5'", code="dated_identifier")
    if provider.kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE and not (endpoint_override or provider.default_endpoint):
        raise ConfigRefused("a locally hosted model needs an address, on the definition or the provider", code="no_endpoint")

    if tenant_id is not None:
        bind_tenant(session, tenant_id)

    definition = ModelDefinition(tenant_id=tenant_id, provider_id=provider_id, model_identifier=model_identifier, display_name=display_name, endpoint_override=endpoint_override, input_price_per_1k=input_price_per_1k, output_price_per_1k=output_price_per_1k, cache_read_price_per_1k=cache_read_price_per_1k, cache_write_price_per_1k=cache_write_price_per_1k, context_window=context_window)
    session.add(definition)
    session.flush()

    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER, action="model_definition_created", entity_type="model_definition", entity_id=definition.id, after={"model_identifier": model_identifier, "provider": provider.name, "endpoint_override": endpoint_override, "scope": "tenant" if tenant_id else "global"}))
    session.flush()
    log.info("model_definition_created", model_identifier=model_identifier, provider=provider.name, scope="tenant" if tenant_id else "global")
    return definition


# ============================================================= assignments ===
def task_bucket_for(task_key: str) -> str:
    """The scope discipline, as a lookup."""
    return TaskBucket.CONSEQUENTIAL if task_key in CONSEQUENTIAL_TASKS else TaskBucket.BOUNDED


def propose_assignment(session: Session, *, tenant_id: uuid.UUID, task_key: str, model_definition_id: uuid.UUID, actor_id: uuid.UUID, estimated_cost_per_report: float | None = None) -> TaskModelAssignment:
    """Propose a model for one step of one lab's pipeline."""
    if task_key not in TaskKey.values():
        raise ConfigRefused(f"unknown task {task_key!r}", code="bad_task")

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ConfigRefused(f"no tenant {tenant_id}", code="no_tenant")

    # `task_model_assignment` is tenant-scoped, so the session narrows onto the lab before writing.
    bind_tenant(session, tenant_id)

    definition = session.get(ModelDefinition, model_definition_id)
    if definition is None:
        raise ConfigRefused(f"no model_definition {model_definition_id}", code="no_definition")
    if definition.tenant_id is not None and definition.tenant_id != tenant_id:
        raise ConfigRefused("that model belongs to another lab", code="cross_tenant_definition")
    if not definition.is_active:
        raise ConfigRefused(f"model {definition.display_name!r} is retired", code="inactive_definition")

    provider = session.get(ModelProvider, definition.provider_id)
    bucket = task_bucket_for(task_key)

    if bucket == TaskBucket.CONSEQUENTIAL and provider is not None and provider.kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE:
        # Refused at proposal so the admin sees why while choosing,
        # rather than discovering it when activation fails.
        raise ConfigRefused(f"{task_key!r} is a consequential task and stays on a frontier model regardless of local hardware capability. Bounded tasks — routing shortlist, utterance classification, round-trip check — are where a local model belongs.", code="consequential_task_local_model")

    # A cloud provider with a missing key produces an assignment that cannot
    # run. Checked now for the same reason.
    if provider is not None:
        resolve_api_key(provider)

    existing = session.execute(select(TaskModelAssignment).where(TaskModelAssignment.tenant_id == tenant_id, TaskModelAssignment.task_key == task_key, TaskModelAssignment.model_definition_id == model_definition_id, TaskModelAssignment.status == AssignmentStatus.PROPOSED)).scalar_one_or_none()
    if existing is not None:
        return existing

    assignment = TaskModelAssignment(tenant_id=tenant_id, task_key=task_key, task_bucket=bucket, model_definition_id=model_definition_id, status=AssignmentStatus.PROPOSED, estimated_cost_per_report=estimated_cost_per_report)
    session.add(assignment)
    session.flush()

    session.add(TaskModelAssignmentLog(tenant_id=tenant_id, task_key=task_key, model_definition_id=model_definition_id, event=AssignmentEvent.PROPOSED, actor_id=actor_id))
    session.flush()

    log.info("assignment_proposed", tenant_id=str(tenant_id), task_key=task_key, model=definition.model_identifier, bucket=bucket)
    return assignment


@dataclass(frozen=True, slots=True)
class StepConfig:
    """One pipeline step and what currently serves it, for one lab."""

    task_key: str
    task_bucket: str
    is_asr: bool
    active_model: str | None
    active_provider: str | None
    active_assignment_id: uuid.UUID | None
    is_local: bool
    proposed: tuple[tuple[uuid.UUID, str], ...] = field(default=())
    """`(assignment_id, model label)` awaiting an eval run."""

    @property
    def is_configured(self) -> bool:
        return self.active_model is not None


def step_configuration(session: Session, *, tenant_id: uuid.UUID) -> list[StepConfig]:
    """Every pipeline step for one lab, with what serves it."""
    # `task_model_assignment` is tenant-scoped, so an unbound session reads **nothing** — RLS filters it out silently and every step reports as unconfigured.
    bind_tenant(session, tenant_id)

    rows = session.execute(select(TaskModelAssignment, ModelDefinition, ModelProvider).join(ModelDefinition, ModelDefinition.id == TaskModelAssignment.model_definition_id).join(ModelProvider, ModelProvider.id == ModelDefinition.provider_id).where(TaskModelAssignment.tenant_id == tenant_id)).all()

    active: dict[str, tuple[TaskModelAssignment, ModelDefinition, ModelProvider]] = {}
    proposed: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for assignment, definition, provider in rows:
        if assignment.status == AssignmentStatus.ACTIVE:
            active[assignment.task_key] = (assignment, definition, provider)
        elif assignment.status in (AssignmentStatus.PROPOSED, AssignmentStatus.TESTING):
            proposed.setdefault(assignment.task_key, []).append((assignment.id, f"{definition.display_name} ({provider.name})"))

    configuration: list[StepConfig] = []
    for task_key in TaskKey.values():
        entry = active.get(task_key)
        configuration.append(StepConfig(task_key=task_key, task_bucket=task_bucket_for(task_key), is_asr=task_key in ASR_TASKS, active_model=entry[1].display_name if entry else None, active_provider=entry[2].name if entry else None, active_assignment_id=entry[0].id if entry else None, is_local=(entry[2].kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE if entry else False), proposed=tuple(proposed.get(task_key, ()))))
    return configuration


def available_models(session: Session, *, tenant_id: uuid.UUID) -> list[tuple[ModelDefinition, ModelProvider]]:
    """Models this lab may be assigned: the global catalog plus its own."""
    rows = session.execute(select(ModelDefinition, ModelProvider).join(ModelProvider, ModelProvider.id == ModelDefinition.provider_id).where(ModelDefinition.is_active.is_(True), ModelProvider.is_active.is_(True), (ModelDefinition.tenant_id.is_(None)) | (ModelDefinition.tenant_id == tenant_id)).order_by(ModelProvider.name, ModelDefinition.display_name)).all()
    return [(definition, provider) for definition, provider in rows]


def unconfigured_steps(session: Session, *, tenant_id: uuid.UUID) -> list[str]:
    """Steps with nothing active. What stops this lab running."""
    return [c.task_key for c in step_configuration(session, tenant_id=tenant_id) if not c.is_configured]
