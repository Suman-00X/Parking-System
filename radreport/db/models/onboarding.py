"""Tables behind lab onboarding, where a lab's existing reports become templates, vocabulary and rules.

Defines: uploads (ImportBatch, ImportArtifact), proposed templates (TemplateImportCandidate,
TemplateMergeProposal), the historical corpus and its template map (CorpusReport,
CorpusReportTemplateMap), vocabulary mining and its sound-alike findings (LexiconMiningRun,
CollisionAuditFinding), suggested normal statements (BoilerplateCandidate) and the pilot gate
(OnboardingReadinessCheck).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import CandidateReviewStatus, CheckStatus, CollisionClass, CollisionResolution, CollisionSeverity, ImportBatchType, ImportStatus, ImportTrigger, MatchMethod, MergeDecision, ParseStatus, ReviewStatus
from radreport.db.base import Base, TenantScoped, TimestampMixin, enum_check, tenant_fk, tenant_table_args, uuid_pk


class ImportBatch(Base, TenantScoped, TimestampMixin):
    """One execution of the onboarding pipeline, or any later re-run."""

    __tablename__ = "import_batch"
    __table_args__ = tenant_table_args(tenant_fk("submitted_by", "app_user", ondelete="RESTRICT"), tenant_fk("approved_by", "app_user", ondelete="RESTRICT"), enum_check("batch_type", ImportBatchType.values()), enum_check("trigger", ImportTrigger.values()), enum_check("status", ImportStatus.values()), Index("ix_import_batch_status", "tenant_id", "status", "created_at"))

    id: Mapped[uuid.UUID] = uuid_pk()
    batch_type: Mapped[str] = mapped_column(String(24), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    """Which onboarding stage: every onboarding stage."""

    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=ImportStatus.UPLOADING)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Approval is a clinical act."""

    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    blocking_issue_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """Non-zero blocks `applied`. A `block`-severity collision finding is the main source — this is the gate that catches LMC vs LMP."""

    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """The onboarding rollback."""


class ImportArtifact(Base, TenantScoped, TimestampMixin):
    """Each uploaded file, with parse provenance."""

    __tablename__ = "import_artifact"
    __table_args__ = tenant_table_args(tenant_fk("import_batch_id", "import_batch", ondelete="CASCADE"), UniqueConstraint("import_batch_id", "content_hash"), enum_check("parse_status", ParseStatus.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    """ confirms Word/PDF only, so no RTF or RIS-export parser is needed."""

    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parse_warnings: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class TemplateImportCandidate(Base, TenantScoped, TimestampMixin):
    """Parsed but **not yet live**."""

    __tablename__ = "template_import_candidate"
    __table_args__ = tenant_table_args(tenant_fk("import_batch_id", "import_batch", ondelete="CASCADE"), tenant_fk("import_artifact_id", "import_artifact", ondelete="CASCADE"), ForeignKeyConstraint(["merged_into_template_id", "tenant_id"], ["template.id", "template.tenant_id"], ondelete="SET NULL", name="fk_merged_into_template_tenant"), ForeignKeyConstraint(["promoted_template_version_id", "tenant_id"], ["template_version.id", "template_version.tenant_id"], ondelete="SET NULL", name="fk_promoted_template_version_tenant"), enum_check("review_status", CandidateReviewStatus.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    import_artifact_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    proposed_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_json_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    proposed_sections: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    proposed_modality: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_body_region: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_spoken_study_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    parse_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    """Low confidence ⇒ mandatory field-by-field review."""

    review_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=CandidateReviewStatus.PENDING)
    merged_into_template_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    promoted_template_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class TemplateMergeProposal(Base, TenantScoped, TimestampMixin):
    """Near-duplicate detection."""

    __tablename__ = "template_merge_proposal"
    __table_args__ = tenant_table_args(tenant_fk("import_batch_id", "import_batch", ondelete="CASCADE"), ForeignKeyConstraint(["template_a_id", "tenant_id"], ["template.id", "template.tenant_id"], ondelete="CASCADE", name="fk_template_a_tenant"), ForeignKeyConstraint(["template_b_id", "tenant_id"], ["template.id", "template.tenant_id"], ondelete="CASCADE", name="fk_template_b_tenant"), tenant_fk("decided_by", "app_user", ondelete="RESTRICT"), enum_check("decision", MergeDecision.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    template_a_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    template_b_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    similarity_score: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    differing_fields: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, server_default=MergeDecision.PENDING)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """Radiologist."""


class CorpusReport(Base, TenantScoped, TimestampMixin):
    """Historical signed reports with no matching audio. Your largest asset."""

    __tablename__ = "corpus_report"
    __table_args__ = tenant_table_args(tenant_fk("import_batch_id", "import_batch", ondelete="CASCADE"), tenant_fk("radiologist_id", "radiologist_profile", ondelete="SET NULL"), UniqueConstraint("tenant_id", "external_report_id"), Index("ix_corpus_report_referrer", "tenant_id", "referring_doctor"))

    id: Mapped[uuid.UUID] = uuid_pk()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    external_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_text: Mapped[str] = mapped_column(Text, nullable=False)
    report_date: Mapped[dt.date | None] = mapped_column(DateTime(timezone=False), nullable=True)
    radiologist_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    referring_doctor: Mapped[str | None] = mapped_column(Text, nullable=True)
    patient_sex: Mapped[str | None] = mapped_column(String(1), nullable=True)
    patient_age_years: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    """Exemplar retrieval for routing and few-shot. Runs on a local embedding model, so prices this at ₹0."""

    is_deidentified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """**Gates every external API call.** Checked in the LLM adapter, not trusted from the caller."""


class CorpusReportTemplateMap(Base, TenantScoped, TimestampMixin):
    """Derived, not supplied."""

    __tablename__ = "corpus_report_template_map"
    __table_args__ = tenant_table_args(tenant_fk("corpus_report_id", "corpus_report", ondelete="CASCADE"), tenant_fk("template_id", "template", ondelete="CASCADE"), UniqueConstraint("corpus_report_id", "template_id"), enum_check("match_method", MatchMethod.values()), Index("ix_corpus_map_verified", "tenant_id", "template_id", postgresql_where=text("is_verified")))

    id: Mapped[uuid.UUID] = uuid_pk()
    corpus_report_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    match_method: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class LexiconMiningRun(Base, TenantScoped, TimestampMixin):
    """One vocabulary-mining run over a lab's corpus."""

    __tablename__ = "lexicon_mining_run"
    __table_args__ = tenant_table_args(tenant_fk("import_batch_id", "import_batch", ondelete="CASCADE"))

    id: Mapped[uuid.UUID] = uuid_pk()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    corpus_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terms_extracted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terms_new: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terms_auto_accepted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terms_pending_review: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pass_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    """The term mining↔verbatim annotation loop is designed, not accidental: pass 1 uses declared shorthand only, the verbatim annotation stage's ASR run mines observed variants, pass 2 re-runs."""

    run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class CollisionAuditFinding(Base, TenantScoped, TimestampMixin):
    """The gate that catches LMC vs LMP."""

    __tablename__ = "collision_audit_finding"
    __table_args__ = tenant_table_args(
        tenant_fk("import_batch_id", "import_batch", ondelete="CASCADE"),
        tenant_fk("resolved_by", "app_user", ondelete="RESTRICT"),
        # `lexicon_term` is nullable-tenant, and a finding may reference a `spoken_study_code` with no term row at all — hence plain, nullable FKs plus the denormalised `label_a`/`label_b`.
        ForeignKeyConstraint(["term_a_id"], ["lexicon_term.id"], ondelete="SET NULL"),
        ForeignKeyConstraint(["term_b_id"], ["lexicon_term.id"], ondelete="SET NULL"),
        enum_check("collision_class", CollisionClass.values()),
        enum_check("severity", CollisionSeverity.values()),
        enum_check("resolution", CollisionResolution.values()),
        Index("ix_collision_blocking", "tenant_id", postgresql_where=text("severity = 'block' AND resolution = 'pending'")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    term_a_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    term_b_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    label_a: Mapped[str] = mapped_column(Text, nullable=False)
    label_b: Mapped[str] = mapped_column(Text, nullable=False)
    """Denormalised so a finding stays readable after a term is renamed, and so `spoken_study_code` collisions (which have no `lexicon_term` row) fit the same table."""

    phonetic_distance: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    collision_class: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    """`block` if both map to different templates or opposite clinical meanings."""

    resolution: Mapped[str] = mapped_column(String(32), nullable=False, server_default=CollisionResolution.PENDING)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class BoilerplateCandidate(Base, TenantScoped, TimestampMixin):
    """Mined normal statements, proposed for `default_normal_text`."""

    __tablename__ = "boilerplate_candidate"
    __table_args__ = tenant_table_args(tenant_fk("template_field_id", "template_field", ondelete="CASCADE"), enum_check("review_status", ReviewStatus.values()), Index("ix_boilerplate_share", "tenant_id", "template_field_id", "corpus_share"))

    id: Mapped[uuid.UUID] = uuid_pk()
    template_field_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    candidate_text: Mapped[str] = mapped_column(Text, nullable=False)
    corpus_frequency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corpus_share: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=ReviewStatus.PENDING)


class OnboardingReadinessCheck(Base, TenantScoped, TimestampMixin):
    """/. The gate between onboarding and pilot."""

    __tablename__ = "onboarding_readiness_check"
    __table_args__ = tenant_table_args(enum_check("status", CheckStatus.values()), UniqueConstraint("tenant_id", "check_id", "evaluated_at"), Index("ix_readiness_latest", "tenant_id", "check_id", "evaluated_at"))

    id: Mapped[uuid.UUID] = uuid_pk()
    check_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    measured_value: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    threshold: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evaluated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
