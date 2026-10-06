"""Tables for the draft a run produces: which template it used, what it said, where each value came from, and what was flagged.

Defines: the template choice (RoutingDecision), the draft and its values (ReportDraft,
ReportFieldValue), the audio each value traces back to (ProvenanceSpan), what the checks found
(VerificationFinding), urgent-finding rules and alerts (CriticalFindingRule,
CriticalFindingAlert) and the record of unreviewed releases (AutonomyObservation).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import AlertSeverity, AssertionStatus, CheckType, DraftStatus, FillSource, HumanVerdict, Laterality, PatternType, Severity, SeverityGrade, Stage1FilterSource
from radreport.db.base import Base, TenantScoped, TimestampMixin, enum_check, tenant_fk, tenant_table_args, uuid_pk


class RoutingDecision(Base, TenantScoped, TimestampMixin):
    """Why this template was chosen. Auditable, and the training set for the distilled router (GA)."""

    __tablename__ = "routing_decision"
    __table_args__ = tenant_table_args(tenant_fk("recording_id", "recording", ondelete="CASCADE"), tenant_fk("chosen_template_version_id", "template_version", ondelete="RESTRICT"), ForeignKeyConstraint(["human_chosen_template_version_id", "tenant_id"], ["template_version.id", "template_version.tenant_id"], ondelete="SET NULL", name="fk_human_chosen_template_version_tenant"), enum_check("stage1_filter_source", Stage1FilterSource.values()), Index("ix_routing_decision_recording", "tenant_id", "recording_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    chosen_template_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    attached_module_version_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=True)

    candidate_set: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    """Full shortlist: [{template_version_id, score, reason}]."""

    stage1_filter_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage1_candidate_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)

    orphan_assertion_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    """The wrong-template signal."""

    required_field_gap_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    was_human_overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    human_chosen_template_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Gold label for retraining."""


class ReportDraft(Base, TenantScoped, TimestampMixin):
    """`tenant_id` is denormalised from `study` — this is the table where a leak would matter most, which is exactly why the composite FKs apply here."""

    __tablename__ = "report_draft"
    __table_args__ = tenant_table_args(tenant_fk("recording_id", "recording", ondelete="CASCADE"), tenant_fk("template_version_id", "template_version", ondelete="RESTRICT"), tenant_fk("pipeline_run_id", "pipeline_run", ondelete="SET NULL"), tenant_fk("routing_decision_id", "routing_decision", ondelete="SET NULL"), enum_check("status", DraftStatus.values()), Index("ix_report_draft_status", "tenant_id", "status"), Index("ix_report_draft_flagged", "tenant_id", "flagged_field_count"), Index("ix_report_draft_recording", "tenant_id", "recording_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    template_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    routing_decision_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    structured_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    overall_confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    flagged_field_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """Drives review UI ordering — flagged-first."""

    prompt_bundle_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """Every model id + version used.: a vendor-side change is a pipeline change, and this is where you prove which one produced a given draft."""

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=DraftStatus.GENERATED)


class ReportFieldValue(Base, TenantScoped, TimestampMixin):
    """The atomic unit."""

    __tablename__ = "report_field_value"
    __table_args__ = tenant_table_args(tenant_fk("report_draft_id", "report_draft", ondelete="CASCADE"), tenant_fk("template_field_id", "template_field", ondelete="RESTRICT"), UniqueConstraint("report_draft_id", "template_field_id"), enum_check("assertion_status", AssertionStatus.values()), enum_check("laterality", Laterality.values()), enum_check("fill_source", FillSource.values()), Index("ix_field_value_flagged", "tenant_id", "report_draft_id", postgresql_where=text("is_flagged")))

    id: Mapped[uuid.UUID] = uuid_pk()
    report_draft_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    template_field_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    value_unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_enum: Mapped[str | None] = mapped_column(Text, nullable=True)

    assertion_status: Mapped[str] = mapped_column(String(16), nullable=False)
    """Never a bare boolean. "not mentioned" and "explicitly absent" are different clinical claims."""

    laterality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """Isolated for validation."""

    fill_source: Mapped[str] = mapped_column(String(24), nullable=False)
    """Audit-critical."""

    is_grounded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    is_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flag_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)


class ProvenanceSpan(Base, TenantScoped):
    """Invariant I1, made physical."""

    __tablename__ = "provenance_span"
    __table_args__ = tenant_table_args(tenant_fk("report_field_value_id", "report_field_value", ondelete="CASCADE"), tenant_fk("transcript_id", "transcript", ondelete="RESTRICT"), tenant_fk("utterance_id", "transcript_utterance", ondelete="SET NULL"), Index("ix_provenance_field_value", "tenant_id", "report_field_value_id"), Index("ix_provenance_span_utterance", "tenant_id", "utterance_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    report_field_value_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    transcript_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    utterance_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    """Powers per-field click-to-listen in the review UI."""

    audio_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class VerificationFinding(Base, TenantScoped, TimestampMixin):
    """Output of validators and (Beta) the LLM critic."""

    __tablename__ = "verification_finding"
    __table_args__ = tenant_table_args(tenant_fk("report_draft_id", "report_draft", ondelete="CASCADE"), tenant_fk("report_field_value_id", "report_field_value", ondelete="CASCADE"), enum_check("check_type", CheckType.values()), enum_check("severity", Severity.values()), enum_check("human_verdict", HumanVerdict.values()), Index("ix_verification_draft", "tenant_id", "report_draft_id", "severity"))

    id: Mapped[uuid.UUID] = uuid_pk()
    report_draft_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    report_field_value_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    check_id: Mapped[str] = mapped_column(Text, nullable=False)
    check_type: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    """`block` halts autonomy."""

    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resolved_by_iteration: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    human_verdict: Mapped[str] = mapped_column(String(16), nullable=False, server_default=HumanVerdict.UNREVIEWED)
    """Tunes verifier precision over time."""


class CriticalFindingRule(Base, TenantScoped, TimestampMixin):
    """Radiologist-owned, not engineer-owned."""

    __tablename__ = "critical_finding_rule"
    __table_args__ = tenant_table_args(tenant_fk("approved_by", "app_user", ondelete="RESTRICT"), UniqueConstraint("tenant_id", "code"), enum_check("pattern_type", PatternType.values()), enum_check("severity", AlertSeverity.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    finding_label: Mapped[str] = mapped_column(Text, nullable=False)
    pattern_type: Mapped[str] = mapped_column(String(16), nullable=False)
    """Lexical first; LLM as a recall net."""

    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    negation_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    """"no pneumothorax" must not fire."""

    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    sla_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    escalation_path: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    requires_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class CriticalFindingAlert(Base, TenantScoped):
    """Alerts bypass the normal queue entirely."""

    __tablename__ = "critical_finding_alert"
    __table_args__ = tenant_table_args(tenant_fk("recording_id", "recording", ondelete="CASCADE"), tenant_fk("rule_id", "critical_finding_rule", ondelete="RESTRICT"), tenant_fk("utterance_id", "transcript_utterance", ondelete="SET NULL"), tenant_fk("acknowledged_by", "app_user", ondelete="RESTRICT"), enum_check("outcome", HumanVerdict.values()), Index("ix_alert_sla", "tenant_id", "sla_due_at", postgresql_where=text("not is_breach")), Index("ix_critical_finding_alert_recording", "tenant_id", "recording_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    utterance_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)

    detected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    sla_due_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_breach: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, server_default=HumanVerdict.UNREVIEWED)


class AutonomyObservation(Base, TenantScoped):
    """One row per signed report in an accruing or granted class."""

    __tablename__ = "autonomy_observation"
    __table_args__ = (ForeignKeyConstraint(["autonomy_class_id", "tenant_id"], ["autonomy_class.id", "autonomy_class.tenant_id"], ondelete="CASCADE"), ForeignKeyConstraint(["final_report_id", "tenant_id"], ["final_report.id", "final_report.tenant_id"], ondelete="CASCADE"), enum_check("severity_grade", SeverityGrade.values()), UniqueConstraint("final_report_id"), Index("ix_autonomy_obs_class", "tenant_id", "autonomy_class_id", "observed_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    autonomy_class_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    final_report_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    severity_grade: Mapped[str] = mapped_column(String(4), nullable=False)
    is_cse: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """G3 or G4."""

    counted_in_accrual: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """Excludes gold-set overlap — an item that trained or tuned the pipeline cannot also serve as evidence that it works."""

    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
