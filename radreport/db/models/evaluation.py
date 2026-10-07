"""Tables for measuring accuracy against a held-back set of reports.

Defines: the gold set and its members (EvalSet, EvalItem), a measurement run (EvalRun) and its
per-item outcomes (EvalResult).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKeyConstraint, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import AudioQualityBucket, CaptureDeviceClass, TaskKey
from radreport.db.base import Base, TenantOptional, TimestampMixin, enum_check, uuid_pk


class EvalSet(Base, TenantOptional, TimestampMixin):
    """A held-back set of reports that accuracy is measured against."""

    __tablename__ = "eval_set"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    stratification_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    is_canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """Redundant with `tenant_id IS NULL` on purpose: it makes the intent readable in a query and lets the gate logic assert rather than infer."""


class EvalItem(Base, TenantOptional, TimestampMixin):
    """One graded report in an eval set; `capture_device_class` is denormalised for the partition."""

    __tablename__ = "eval_item"
    __table_args__ = (
        # `eval_set` is nullable-tenant (canonical sets have no tenant), so these are plain FKs.
        ForeignKeyConstraint(["eval_set_id"], ["eval_set.id"], ondelete="CASCADE"),
        # Through the source lab: recording is partitioned by lab, so it is reached by (id, tenant_id).
        ForeignKeyConstraint(["recording_id", "source_tenant_id"], ["recording.id", "recording.tenant_id"], ondelete="RESTRICT", name="fk_eval_item_recording_source"),
        ForeignKeyConstraint(["source_tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        enum_check("audio_quality_bucket", AudioQualityBucket.values()),
        enum_check("capture_device_class", CaptureDeviceClass.values()),
        UniqueConstraint("eval_set_id", "recording_id"),
        Index("ix_eval_item_strata", "eval_set_id", "capture_device_class", "audio_quality_bucket"),
        # a canonical gate run must be able to report per-lab breakdowns.
        Index("ix_eval_item_source_tenant", "eval_set_id", "source_tenant_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    eval_set_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    source_tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    """Which lab this item came from, even when the set itself is canonical (`tenant_id IS NULL`)."""

    gold_transcript_verbatim: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Disfluencies retained. Human labor, and the project's critical path — everything forward-looking waits on the 150 `current` items."""

    gold_utterance_labels: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    gold_template_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    gold_structured_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    gold_codeword_occurrences: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    audio_quality_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    capture_device_class: Mapped[str] = mapped_column(String(32), nullable=False)
    annotated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class EvalRun(Base, TenantOptional, TimestampMixin):
    """One eval run, scopeable to a single `task_key` and batch-capable."""

    __tablename__ = "eval_run"
    __table_args__ = (ForeignKeyConstraint(["eval_set_id"], ["eval_set.id"], ondelete="RESTRICT"), enum_check("task_key", TaskKey.values()), Index("ix_eval_run_gate", "eval_set_id", "started_at", postgresql_where=text("is_release_gate")))

    id: Mapped[uuid.UUID] = uuid_pk()
    eval_set_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    task_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """NULL = full-pipeline run."""

    is_smoke_subset: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """Fix 2: a ~30-item subset for iteration, the full 150 only on release candidates."""

    used_batch_api: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """All metrics. ROUTE_TOP1 is reported **per template, never averaged only** — and, per source tenant too."""

    total_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    is_release_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class EvalResult(Base, TenantOptional):
    """One metric value for one item in one run."""

    __tablename__ = "eval_result"
    __table_args__ = (ForeignKeyConstraint(["eval_run_id"], ["eval_run.id"], ondelete="CASCADE"), ForeignKeyConstraint(["eval_item_id"], ["eval_item.id"], ondelete="CASCADE"), UniqueConstraint("eval_run_id", "eval_item_id", "metric_key"), Index("ix_eval_result_metric", "eval_run_id", "metric_key"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    eval_run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    eval_item_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    metric_key: Mapped[str] = mapped_column(Text, nullable=False)
    metric_value: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    error_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
