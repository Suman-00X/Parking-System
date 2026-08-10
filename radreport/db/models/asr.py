"""Tables for a speech-recognition run and the transcript it produces.

Defines: the run (AsrRun), its timed pieces of audio (AsrSegment), and the transcript built from
them (Transcript, TranscriptUtterance).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import AsrStatus, LabelSource, ReconciliationMethod, TranscriptStage, UtteranceLabel
from radreport.db.base import Base, TenantScoped, enum_check, tenant_fk, tenant_table_args, uuid_pk


class AsrRun(Base, TenantScoped):
    """One execution of one engine against one recording."""

    __tablename__ = "asr_run"
    __table_args__ = tenant_table_args(
        tenant_fk("recording_id", "recording", ondelete="CASCADE"),
        # `lexicon_set` is nullable-tenant, so a plain FK.
        ForeignKeyConstraint(["keyterm_set_id"], ["lexicon_set.id"], ondelete="RESTRICT"),
        UniqueConstraint("recording_id", "engine", "engine_version", "config_hash"),
        enum_check("status", AsrStatus.values()),
        Index("ix_asr_run_tenant_recording", "tenant_id", "recording_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    engine: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    """Pinned. Never "latest" — treats a vendor-side change as a pipeline change that must clear the release gate."""

    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    keyterm_set_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    overall_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    wer_estimate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    """Populated only for eval items."""

    insertion_rate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    """Insertion rate, tracked separately and **never folded into the word error rate**: on Indian-accented speech insertions were 50.7% of large-v3's errors."""

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=AsrStatus.PENDING)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=sql_text("now()"))


class AsrSegment(Base, TenantScoped):
    """Word- or phrase-level output. Partitioned by month."""

    __tablename__ = "asr_segment"
    __table_args__ = (ForeignKeyConstraint(["asr_run_id", "tenant_id"], ["asr_run.id", "asr_run.tenant_id"], ondelete="CASCADE"), Index("ix_asr_segment_run_seq", "tenant_id", "asr_run_id", "seq"), {"postgresql_partition_by": "RANGE (created_at)"})

    # A partitioned table's primary key must include the partition key, so the
    # PK is (id, created_at) rather than (id).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    asr_run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    speaker_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary_speaker: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Resolved against voice enrollment (`radiologist_profile.voice_embedding`)."""

    alternatives: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=sql_text("now()"), primary_key=True)


class Transcript(Base, TenantScoped):
    """The reconciled, normalised transcript. Versioned per recording."""

    __tablename__ = "transcript"
    __table_args__ = tenant_table_args(tenant_fk("recording_id", "recording", ondelete="CASCADE"), UniqueConstraint("recording_id", "version", "stage"), enum_check("stage", TranscriptStage.values()), enum_check("reconciliation_method", ReconciliationMethod.values()), Index("ix_transcript_current", "tenant_id", "recording_id", postgresql_where=sql_text("is_current")))

    id: Mapped[uuid.UUID] = uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    """Full text. Char offsets into this string are what `provenance_span` cites, and what the grounding check verifies verbatim (I1)."""

    source_asr_run_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=False)
    reconciliation_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    disagreement_score: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    """Engine disagreement — a strong uncertainty signal."""

    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=sql_text("now()"))


class TranscriptUtterance(Base, TenantScoped):
    """Segmentation and classification."""

    __tablename__ = "transcript_utterance"
    __table_args__ = tenant_table_args(
        tenant_fk("transcript_id", "transcript", ondelete="CASCADE"),
        # Self-reference: the retracted half of a self-correction points at the
        # utterance that replaced it. Composite, like every other tenant-scoped FK.
        ForeignKeyConstraint(["superseded_by_id", "tenant_id"], ["transcript_utterance.id", "transcript_utterance.tenant_id"], ondelete="SET NULL", name="fk_superseded_by_tenant"),
        enum_check("label", UtteranceLabel.values()),
        enum_check("label_source", LabelSource.values()),
        UniqueConstraint("transcript_id", "seq"),
        Index("ix_utterance_included", "tenant_id", "transcript_id", postgresql_where=sql_text("is_included_downstream")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    transcript_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    label: Mapped[str] = mapped_column(String(24), nullable=False)
    label_confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    label_source: Mapped[str] = mapped_column(String(24), nullable=False)

    contains_clinical_tokens: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """Guardrail: a non-report label carrying clinical tokens escalates rather than being dropped."""

    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Set on the *retracted* half of a self-correction."""

    is_included_downstream: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """Computed from `label` + guardrails."""
