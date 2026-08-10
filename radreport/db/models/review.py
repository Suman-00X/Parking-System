"""Tables for the review step, including every edit a reviewer makes -- the system's most valuable feedback.

Defines: each saved version of a report (ReportRevision), the individual edits
(EditEvent), the signed result (FinalReport) and a reviewer's verdict on how useful the draft
was (DraftUsefulnessReport).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import EditType, ErrorCategory, ExportStatus, PathType, ReviewerRole, SeverityGrade
from radreport.db.base import Base, TenantScoped, enum_check, tenant_fk, tenant_table_args, uuid_pk


class ReportRevision(Base, TenantScoped):
    """One saved version of a report during review."""

    __tablename__ = "report_revision"
    __table_args__ = tenant_table_args(tenant_fk("report_draft_id", "report_draft", ondelete="CASCADE"), tenant_fk("revised_by", "app_user", ondelete="RESTRICT"), UniqueConstraint("report_draft_id", "revision_number"), enum_check("reviewer_role", ReviewerRole.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    report_draft_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    revised_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    reviewer_role: Mapped[str] = mapped_column(String(24), nullable=False)
    """Two-layer supervision."""

    structured_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    edit_distance_from_prev: Mapped[int] = mapped_column(Integer, nullable=False)

    active_edit_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    """**Focus time, not wall clock.** The primary value-metric input: the commercial argument rests entirely on it, and the corrected break-even bar is 18–36 seconds saved per report."""

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EditEvent(Base, TenantScoped):
    """Field-level diffs — the ASR fine-tuning and correction corpus."""

    __tablename__ = "edit_event"
    __table_args__ = (ForeignKeyConstraint(["report_revision_id", "tenant_id"], ["report_revision.id", "report_revision.tenant_id"], ondelete="CASCADE"), ForeignKeyConstraint(["report_field_value_id", "tenant_id"], ["report_field_value.id", "report_field_value.tenant_id"], ondelete="SET NULL"), enum_check("edit_type", EditType.values()), enum_check("error_category", ErrorCategory.values()), enum_check("severity_grade", SeverityGrade.values()), Index("ix_edit_event_category", "tenant_id", "error_category", "severity_grade"), Index("ix_edit_event_training", "tenant_id", postgresql_where=text("is_training_eligible")), {"postgresql_partition_by": "RANGE (created_at)"})

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    report_revision_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    report_field_value_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    edit_type: Mapped[str] = mapped_column(String(24), nullable=False)
    before_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    audio_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript_char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript_char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    severity_grade: Mapped[str | None] = mapped_column(String(4), nullable=True)
    """Assigned on the sampling schedule: every report weeks 1–4, then 1-in-5, 1-in-10, 1-in-20."""

    is_training_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    """Excludes eval-set items."""


class FinalReport(Base, TenantScoped):
    """Immutable, content-hashed — a legal medical record."""

    __tablename__ = "final_report"
    __table_args__ = tenant_table_args(
        tenant_fk("study_id", "study", ondelete="RESTRICT"),
        tenant_fk("report_draft_id", "report_draft", ondelete="RESTRICT"),
        tenant_fk("final_revision_id", "report_revision", ondelete="RESTRICT"),
        tenant_fk("signed_by", "app_user", ondelete="RESTRICT"),
        # Addendum chain, self-referential.
        ForeignKeyConstraint(["amends_report_id", "tenant_id"], ["final_report.id", "final_report.tenant_id"], ondelete="RESTRICT", name="fk_amends_report_tenant"),
        tenant_fk("autonomy_class_id", "autonomy_class", ondelete="RESTRICT"),
        enum_check("path_type", PathType.values()),
        enum_check("export_status", ExportStatus.values()),
        # A report with no reviewer is legitimate only on the autonomous path, and an autonomous report must name the class that permitted it.
        CheckConstraint("(path_type = 'autonomous' AND final_revision_id IS NULL AND autonomy_class_id IS NOT NULL) OR (path_type <> 'autonomous' AND (final_revision_id IS NOT NULL OR amends_report_id IS NOT NULL))", name="autonomous_iff_unreviewed"),
        Index("ix_final_report_signed", "tenant_id", "signed_at"),
        Index("ix_final_report_path", "tenant_id", "path_type", "signed_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    study_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    report_draft_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    final_revision_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Null exactly on the autonomous path."""

    signed_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    """On the autonomous path, the radiologist who dictated it."""

    signed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    structured_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """Tamper evidence."""

    path_type: Mapped[str] = mapped_column(String(32), nullable=False)
    """Autonomy audit: assistant-reviewed, radiologist-only, or autonomous?"""

    autonomy_class_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Which grant permitted an autonomous release. Set only on that path."""

    amends_report_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    exported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    export_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=ExportStatus.PENDING)
    """How a signed report was sent out: HL7 v2 ORU^R01 or FHIR."""

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DraftUsefulnessReport(Base, TenantScoped):
    """The one-click "this draft was useless" — and actually track it."""

    __tablename__ = "draft_usefulness_report"
    __table_args__ = tenant_table_args(tenant_fk("report_draft_id", "report_draft", ondelete="CASCADE"), tenant_fk("reported_by", "app_user", ondelete="RESTRICT"), UniqueConstraint("report_draft_id", "reported_by"))

    id: Mapped[uuid.UUID] = uuid_pk()
    report_draft_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    reported_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    was_useless: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
