"""Table for a captured audio recording -- one recording, one report.

Defines: Recording, whose content hash is unique so re-uploading the same file cannot create a
second copy.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import AudioFormat, CaptureDeviceClass
from radreport.db.base import Base, TenantScoped, TimestampMixin, enum_check, tenant_fk, tenant_table_args, uuid_pk


class Recording(Base, TenantScoped, TimestampMixin):
    """One captured recording; `content_hash` is unique to block double-processing on retry."""

    __tablename__ = "recording"
    __table_args__ = tenant_table_args(
        tenant_fk("study_id", "study", ondelete="RESTRICT"),
        tenant_fk("radiologist_id", "radiologist_profile", ondelete="RESTRICT"),
        tenant_fk("study_code_template_id", "template", ondelete="SET NULL"),
        UniqueConstraint("study_id"),  # enforces 1 recording : 1 report
        UniqueConstraint("tenant_id", "content_hash"),
        enum_check("capture_device_class", CaptureDeviceClass.values()),
        enum_check("audio_format", AudioFormat.values()),
        Index("ix_recording_tenant_device_class", "tenant_id", "capture_device_class"),
        Index("ix_recording_tenant_uploaded", "tenant_id", "uploaded_at"),
        Index("ix_recording_training_eligible", "tenant_id", postgresql_where=text("is_training_corpus_eligible")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    study_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    radiologist_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    duration_seconds: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    codec: Mapped[str | None] = mapped_column(Text, nullable=True)
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    measured_snr_db: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    """Computed at ingest; correlates with error rate."""

    silence_ratio: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    """Quality gate."""

    device_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_device_class: Mapped[str] = mapped_column(String(32), nullable=False)
    """Partitions the gold set and stratifies every metric."""

    is_push_to_talk: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """PTT eliminates most pre-roll/aside contamination at source — notes a record button solves in hardware what would otherwise need a classifier."""

    audio_format: Mapped[str] = mapped_column(String(8), nullable=False)
    """Flac or wav. Lossy is rejected at ingest — it irreversibly destroys the spectral detail ASR fine-tuning depends on."""

    study_code_spoken_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    study_code_template_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    study_code_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    end_marker_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    is_training_corpus_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """**derived, not set**. Computable only as:"""

    phi_scrub_completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """De-identification covers prompts, not audio: a spoken patient name stays in the retained FLAC forever, so it is handled here."""

    retention_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """NULL = indefinite (clinical archive)."""
