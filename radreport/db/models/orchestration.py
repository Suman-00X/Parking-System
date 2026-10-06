"""Tables recording each pipeline run, the stages inside it, and an append-only audit trail.

Defines: PipelineRun, StageExecution and AuditLog.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import ActorType, PipelineTrigger, RunStatus
from radreport.db.base import Base, TenantScoped, TimestampMixin, enum_check, tenant_fk, tenant_table_args, uuid_pk


class PipelineRun(Base, TenantScoped, TimestampMixin):
    """One end-to-end run of the pipeline over a recording."""

    __tablename__ = "pipeline_run"
    __table_args__ = tenant_table_args(tenant_fk("recording_id", "recording", ondelete="CASCADE"), enum_check("trigger", PipelineTrigger.values()), enum_check("status", RunStatus.values()), Index("ix_pipeline_run_status", "tenant_id", "status", "created_at"), Index("ix_pipeline_run_shadow", "tenant_id", postgresql_where=text("is_shadow")), Index("ix_pipeline_run_recording", "tenant_id", "recording_id"), Index("ix_pipeline_run_cost", "tenant_id", "created_at", postgresql_include=["total_cost_usd"], postgresql_where=text("not is_shadow")))

    id: Mapped[uuid.UUID] = uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)

    is_shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """Shadow output never reaches users."""

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=RunStatus.QUEUED)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    total_cost_usd: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False, server_default=text("0"))
    """Per-report unit economics, and the hard budget cap: the orchestrator aborts rather than letting one pathological recording run up an unbounded bill."""

    budget_cap_usd: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    error_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class StageExecution(Base, TenantScoped):
    """One attempt of one stage."""

    __tablename__ = "stage_execution"
    __table_args__ = tenant_table_args(
        tenant_fk("pipeline_run_id", "pipeline_run", ondelete="CASCADE"),
        # Denormalised for cost attribution per template and per
        # radiologist — cheap while these rows are written, awkward to backfill.
        tenant_fk("template_id", "template", ondelete="SET NULL"),
        tenant_fk("radiologist_id", "radiologist_profile", ondelete="SET NULL"),
        UniqueConstraint("pipeline_run_id", "stage_name", "attempt"),
        Index("ix_stage_execution_run", "tenant_id", "pipeline_run_id"),
        Index("ix_stage_execution_cost", "tenant_id", "stage_name", "created_at"),
        Index("ix_stage_execution_task", "tenant_id", "task_key", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    stage_name: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))

    input_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    task_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Which task this stage invoked, so per-task cost and per-task eval line up. The per-task model swap is meaningless without it."""

    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The resolved model identifier actually sent to the provider; together with `model_definition` this is where the pinned version is recorded."""

    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """From `usage.cache_read_input_tokens`."""

    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=RunStatus.RUNNING)

    template_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    radiologist_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class AuditLog(Base):
    """Append-only, on a separate DB role with INSERT-only grants."""

    __tablename__ = "audit_log"
    __table_args__ = (enum_check("actor_type", ActorType.values()), Index("ix_audit_log_entity", "entity_type", "entity_id", "occurred_at"), Index("ix_audit_log_tenant", "tenant_id", "occurred_at"), Index("ix_audit_log_action", "action", "occurred_at"), {"postgresql_partition_by": "RANGE (occurred_at)"})

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"), primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Deliberately carries **no** foreign key: an actor may be an `app_user` or a `platform_user`, which are two disjoint realms."""
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    """`admin_org_selected` is written on every `SET app.current_tenant_id` from a product-admin session."""

    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
