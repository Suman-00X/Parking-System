"""Tables recording which language model runs which step, per lab.

Defines: the vendors (ModelProvider), the models themselves (ModelDefinition), the live
step-to-model choice (TaskModelAssignment) and an append-only history of changes to it
(TaskModelAssignmentLog).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import AssignmentEvent, AssignmentStatus, AuthMethod, ProviderKind, TaskBucket, TaskKey
from radreport.db.base import Base, TenantOptional, TenantScoped, TimestampMixin, enum_check, tenant_table_args, uuid_pk


class ModelProvider(Base, TenantOptional, TimestampMixin):
    """NULL `tenant_id` = a global provider offered to all labs."""

    __tablename__ = "model_provider"
    __table_args__ = (UniqueConstraint("tenant_id", "name"), enum_check("kind", ProviderKind.values()), enum_check("auth_method", AuthMethod.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    default_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False, server_default=AuthMethod.API_KEY)

    api_key_env_var: Mapped[str | None] = mapped_column(Text, nullable=True)
    """**The name of the environment variable, never the key.**"""
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ModelDefinition(Base, TenantOptional, TimestampMixin):
    """A specific model with its pricing — the thing a task is assigned to."""

    __tablename__ = "model_definition"
    __table_args__ = (ForeignKeyConstraint(["provider_id"], ["model_provider.id"], ondelete="RESTRICT"), UniqueConstraint("provider_id", "model_identifier"), Index("ix_model_definition_tenant", "tenant_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    provider_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    model_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

    input_price_per_1k: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    output_price_per_1k: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    cache_write_price_per_1k: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    cache_read_price_per_1k: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    """Not. Without these two the cost accounting cannot price a
 cached call, and the Tier-1 saving (~29% of the LLM bill) would be
 invisible in `stage_execution.cost_usd`."""

    batch_discount_factor: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, server_default=text("1.000"))
    """0.5 on providers offering a Batch API."""

    endpoint_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Genuinely per-tenant: each lab's local box has its own address."""

    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class TaskModelAssignment(Base, TenantScoped, TimestampMixin):
    """+. The current live assignment, one row per (tenant, task)."""

    __tablename__ = "task_model_assignment"
    __table_args__ = tenant_table_args(
        # Both parents are nullable-tenant, so plain FKs (see `lexicon_term`).
        ForeignKeyConstraint(["model_definition_id"], ["model_definition.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["eval_run_id"], ["eval_run.id"], ondelete="RESTRICT"),
        enum_check("task_key", TaskKey.values()),
        enum_check("task_bucket", TaskBucket.values()),
        enum_check("status", AssignmentStatus.values()),
        Index("uq_task_model_assignment_active", "tenant_id", "task_key", unique=True, postgresql_where=text("status = 'active'")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    task_key: Mapped[str] = mapped_column(String(32), nullable=False)
    task_bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    """Determines how much testing is required before activation."""

    model_definition_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=AssignmentStatus.PROPOSED)

    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """The gold-set test result that approved this assignment. Required before `status` can become `active`."""

    estimated_cost_per_report: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """A `platform_user` — model decisions are product-admin only."""

    retired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskModelAssignmentLog(Base, TenantScoped):
    """Append-only audit trail."""

    __tablename__ = "task_model_assignment_log"
    __table_args__ = (
        ForeignKeyConstraint(["model_definition_id"], ["model_definition.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["eval_run_id"], ["eval_run.id"], ondelete="RESTRICT"),
        # `actor_id` is a platform_user: model decisions are product-admin only.
        ForeignKeyConstraint(["actor_id"], ["platform_user.id"], ondelete="RESTRICT"),
        enum_check("event", AssignmentEvent.values()),
        enum_check("task_key", TaskKey.values()),
        Index("ix_assignment_log_task", "tenant_id", "task_key", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    task_key: Mapped[str] = mapped_column(String(32), nullable=False)
    model_definition_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
