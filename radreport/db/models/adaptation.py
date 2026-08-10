"""Tables for adapting the speech models to a lab, and the consent that has to be in place first.

Defines: the typed-out ground truth (VerbatimTranscript), the frozen set of audio a training run
may use (TrainingCorpusSnapshot) and the run itself (ModelAdaptationRun).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import AdaptationMethod, AdaptationStatus, AdaptationTarget, CaptureDeviceClass, VerbatimSource
from radreport.db.base import Base, TenantOptional, TenantScoped, TimestampMixin, enum_check, tenant_fk, tenant_table_args, uuid_pk


class VerbatimTranscript(Base, TenantScoped, TimestampMixin):
    """Token-aligned ground truth — the only asset that can train ASR."""

    __tablename__ = "verbatim_transcript"
    __table_args__ = tenant_table_args(tenant_fk("recording_id", "recording", ondelete="CASCADE"), tenant_fk("annotator_id", "app_user", ondelete="RESTRICT"), UniqueConstraint("recording_id", "source"), enum_check("source", VerbatimSource.values()), enum_check("capture_device_class", CaptureDeviceClass.values()), Index("ix_verbatim_device_class", "tenant_id", "capture_device_class"))

    id: Mapped[uuid.UUID] = uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    """Disfluencies, false starts and corrections **retained**."""

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    """`verified_correction` = reviewer-confirmed ASR output."""

    annotator_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    includes_disfluencies: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """False ⇒ **not training-eligible**. A cleaned-up transcript teaches an ASR model to delete words."""

    audio_duration_seconds: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    """Corpus-hours accounting, against the G1_volume gate: ≥20 h for a global adapter, ≥5 h per speaker."""

    capture_device_class: Mapped[str] = mapped_column(String(32), nullable=False)
    """Denormalised — the filter that matters most."""

    is_eval_set_member: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    """True ⇒ permanently excluded from training."""

    quality_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))


class TrainingCorpusSnapshot(Base, TenantOptional, TimestampMixin):
    """An immutable, reproducible selection."""

    __tablename__ = "training_corpus_snapshot"
    __table_args__ = (
        enum_check("purpose", AdaptationTarget.values()),
        UniqueConstraint("content_hash"),
        # eval-set leakage is silent, unrecoverable, and invalidates every number downstream of it.
        CheckConstraint("excludes_eval_set = true", name="excludes_eval_set_enforced"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    filter_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    item_count: Mapped[int] = mapped_column(Numeric(12, 0), nullable=False)
    total_audio_hours: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    speaker_distribution: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """Guards against single-speaker dominance — G3_speaker_balance reads it."""

    device_class_filter: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    """G4_hardware_homogeneous."""

    excludes_eval_set: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("true"))
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    tenant_consent_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Extends the schema-level rigour to the legal basis: this records that every contributing tenant's consent was live at each item's `uploaded_at`."""

    consent_verification_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ModelAdaptationRun(Base, TenantOptional, TimestampMixin):
    """One run adapting a speech model to a lab's audio."""

    __tablename__ = "model_adaptation_run"
    __table_args__ = (ForeignKeyConstraint(["snapshot_id"], ["training_corpus_snapshot.id"], ondelete="RESTRICT"), ForeignKeyConstraint(["radiologist_id"], ["radiologist_profile.id"], ondelete="RESTRICT"), enum_check("target", AdaptationTarget.values()), enum_check("method", AdaptationMethod.values()), enum_check("status", AdaptationStatus.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    base_model: Mapped[str] = mapped_column(Text, nullable=False)
    base_version: Mapped[str] = mapped_column(Text, nullable=False)
    radiologist_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Per-speaker adapters."""

    hyperparams: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    baseline_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    relative_improvement: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    """Negative ⇒ auto-reject."""

    prerequisite_gates_passed: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The six-gate checklist result; the legal-basis gate is judged against derived eligibility, never a stored flag."""

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
