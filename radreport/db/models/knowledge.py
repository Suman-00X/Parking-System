"""Tables holding what the system knows about a lab: its vocabulary, its spoken codes and its report templates.

Defines: the term lists (LexiconSet, LexiconTerm, LexiconSurfaceVariant), the templates and
their fields (Template, TemplateVersion, TemplateField), per-radiologist pronunciation hints
(SpeakerTermBias) and the report groupings autonomy is granted over (AutonomyClass).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import AbsencePolicy, ExpansionPolicy, FieldDataType, LexiconScope, Sex, TermType, VariantSource
from radreport.db.base import Base, TenantOptional, TenantScoped, TimestampMixin, array_enum_check, enum_check, tenant_fk, tenant_table_args, uuid_pk

#: Variant review statuses the pipeline uses; pending and rejected variants are ignored.
USED_VARIANTS = ("auto_approved", "approved")


class LexiconSet(Base, TenantOptional, TimestampMixin):
    """A versioned bundle of terms used as ASR bias and correction targets."""

    __tablename__ = "lexicon_set"
    __table_args__ = (enum_check("scope", LexiconScope.values()), UniqueConstraint("tenant_id", "name", "version"))

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class LexiconTerm(Base, TenantOptional, TimestampMixin):
    """Canonical domain vocabulary. Mined from historical reports, curated."""

    __tablename__ = "lexicon_term"
    __table_args__ = (
        # Plain FK, not composite: `lexicon_set` has a NULLABLE tenant_id, and a composite FK under MATCH SIMPLE is skipped entirely when any column is NULL — it would silently enforce nothing on exactly the global rows.
        ForeignKeyConstraint(["lexicon_set_id"], ["lexicon_set.id"], ondelete="CASCADE"),
        enum_check("term_type", TermType.values()),
        enum_check("expansion_policy", ExpansionPolicy.values()),
        Index("ix_lexicon_term_phonetic", "phonetic_key_primary"),
        Index("ix_lexicon_term_set", "lexicon_set_id"),
        Index("ix_lexicon_term_code_words", "lexicon_set_id", postgresql_where=text("term_type = 'code_word'")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lexicon_set_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    canonical_form: Mapped[str] = mapped_column(Text, nullable=False)
    short_form: Mapped[str | None] = mapped_column(Text, nullable=True)
    term_type: Mapped[str] = mapped_column(String(24), nullable=False)

    phonetic_key_primary: Mapped[str] = mapped_column(Text, nullable=False)
    phonetic_key_secondary: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)

    frequency_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    radlex_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    snomed_ct_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """PA, RA, CA — needs context resolution."""

    expansion_policy: Mapped[str] = mapped_column(String(24), nullable=False, server_default=ExpansionPolicy.PER_TEMPLATE)


class LexiconSurfaceVariant(Base, TenantOptional, TimestampMixin):
    """How each term actually comes back from ASR."""

    __tablename__ = "lexicon_surface_variant"
    __table_args__ = (ForeignKeyConstraint(["lexicon_term_id"], ["lexicon_term.id"], ondelete="CASCADE"), enum_check("source", VariantSource.values()), enum_check("review_status", ("auto_approved", "approved", "pending", "rejected")), UniqueConstraint("lexicon_term_id", "surface_text"), Index("ix_surface_variant_phonetic", "phonetic_key"), Index("ix_surface_variant_review", "tenant_id", "review_status"))

    id: Mapped[uuid.UUID] = uuid_pk()
    lexicon_term_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    surface_text: Mapped[str] = mapped_column(Text, nullable=False)
    phonetic_key: Mapped[str] = mapped_column(Text, nullable=False)
    observed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    min_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    """Threshold below which a match escalates rather than resolving silently."""

    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    """How sure the miner was that this is the term, 0-1; see knowledge/variant_review.py."""

    review_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="approved")
    """auto_approved and approved variants are used; pending waits for a radiologist; rejected is kept so it is not proposed again."""

    threshold_arm: Mapped[str | None] = mapped_column(String(1), nullable=True)
    """Which threshold arm (A or B) the lab was in when the variant was decided, for the threshold experiment."""

    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutonomyClass(Base, TenantScoped, TimestampMixin):
    """The unit at which autonomy is granted and revoked."""

    __tablename__ = "autonomy_class"
    __table_args__ = tenant_table_args(UniqueConstraint("tenant_id", "code"), enum_check("status", ("not_evaluated", "accruing", "granted", "suspended", "revoked")))

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="not_evaluated")

    baseline_cse_rate: Mapped[float] = mapped_column(Numeric(6, 5), nullable=False)
    """**Measured**, not assumed — the baseline audit produces it by grading 50 already-signed reports G0–G4."""

    ni_margin_pp: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False, server_default=text("1.00"))
    required_n: Mapped[int] = mapped_column(Integer, nullable=False)
    accrued_n: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    observed_cse_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    posterior_prob_ni: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)

    granted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """A `platform_user`: autonomy grants are product-admin decisions."""
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    cusum_statistic: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False, server_default=text("0"))
    cusum_threshold: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    """Post-grant monitor. Revocation is mechanical, not a judgement call."""


class Template(Base, TenantScoped, TimestampMixin):
    """No cross-tenant template sharing by default."""

    __tablename__ = "template"
    __table_args__ = tenant_table_args(tenant_fk("autonomy_class_id", "autonomy_class", ondelete="SET NULL"), UniqueConstraint("tenant_id", "code"), array_enum_check("applicable_sex", Sex.values()), Index("ix_template_usage", "tenant_id", "usage_count_12m"))

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    modality: Mapped[str] = mapped_column(Text, nullable=False)
    body_region: Mapped[str] = mapped_column(Text, nullable=False)

    is_module: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    parent_compatible_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    applicable_sex: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    applicable_age_min: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    applicable_age_max: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    usage_count_12m: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """Power-law head detection — populate this first. V1 ships the top ~20; the head is most of the volume."""

    autonomy_class_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class TemplateVersion(Base, TenantScoped):
    """Reports are legal records; an immutable version history is mandatory."""

    __tablename__ = "template_version"
    __table_args__ = tenant_table_args(
        tenant_fk("template_id", "template", ondelete="CASCADE"),
        tenant_fk("approved_by", "app_user", ondelete="RESTRICT"),
        UniqueConstraint("template_id", "version"),
        # Only the **current** version may claim a spoken study code.
        Index("uq_template_version_current_spoken_code", "tenant_id", "spoken_study_code", unique=True, postgresql_where=text("is_current")),
        Index("ix_template_version_current", "tenant_id", "template_id", postgresql_where=text("is_current")),
        Index("ix_template_version_spoken_phonetic", "tenant_id", "spoken_study_code_phonetic"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    template_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    json_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """Drives constrained decoding."""

    render_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """Field order, headings, house style — per-tenant already."""

    routing_card: Mapped[str] = mapped_column(Text, nullable=False)
    routing_card_embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    trigger_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    study_description_patterns: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    spoken_study_code: Mapped[str] = mapped_column(Text, nullable=False)
    """The phrase the radiologist says. The primary routing anchor."""

    spoken_study_code_phonetic: Mapped[str] = mapped_column(Text, nullable=False)
    """Double-metaphone key, collision-audited across the full set."""

    spoken_study_code_variants: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    approved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class TemplateField(Base, TenantScoped, TimestampMixin):
    """One template field; `absence_policy` defaults to `leave_blank_flag` and V1 auto-fills nothing."""

    __tablename__ = "template_field"
    __table_args__ = tenant_table_args(tenant_fk("template_version_id", "template_version", ondelete="CASCADE"), UniqueConstraint("template_version_id", "field_key"), enum_check("data_type", FieldDataType.values()), enum_check("absence_policy", AbsencePolicy.values()), Index("ix_template_field_section", "tenant_id", "template_version_id", "section", "seq"))

    id: Mapped[uuid.UUID] = uuid_pk()
    template_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False)
    """Parallel extraction unit — extraction runs k=3 samples per section."""

    display_label: Mapped[str] = mapped_column(Text, nullable=False)
    data_type: Mapped[str] = mapped_column(String(24), nullable=False)
    enum_values: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    absence_policy: Mapped[str] = mapped_column(String(24), nullable=False, server_default=AbsencePolicy.LEAVE_BLANK_FLAG)
    default_normal_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    blanket_normal_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    is_critical: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """Never auto-filled, under any circumstance; always human-confirmed."""

    seq: Mapped[int] = mapped_column(Integer, nullable=False)


class SpeakerTermBias(Base, TenantScoped, TimestampMixin):
    """Per-radiologist vocabulary and error patterns, from the flywheel."""

    __tablename__ = "speaker_term_bias"
    __table_args__ = tenant_table_args(tenant_fk("radiologist_id", "radiologist_profile", ondelete="CASCADE"), ForeignKeyConstraint(["lexicon_term_id"], ["lexicon_term.id"], ondelete="CASCADE"), UniqueConstraint("radiologist_id", "lexicon_term_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    radiologist_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    lexicon_term_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    boost_weight: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    observed_error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PotentialLexiconTerm(Base, TenantScoped):
    """A phrase radiologists keep typing into reports that the lab's lexicon does not know, waiting for one of them to approve it."""

    __tablename__ = "potential_lexicon_term"
    __table_args__ = tenant_table_args(UniqueConstraint("tenant_id", "normalized_text"), tenant_fk("decided_by", "app_user", ondelete="SET NULL"), ForeignKeyConstraint(["lexicon_set_version_id"], ["lexicon_set.id"], ondelete="SET NULL"), enum_check("status", ("pending", "approved", "rejected")), Index("ix_potential_lexicon_term_review", "tenant_id", "status", "frequency"))

    id: Mapped[uuid.UUID] = uuid_pk()
    surface_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    term_type: Mapped[str] = mapped_column(String(24), nullable=False)
    frequency: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    contexts: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    """Up to three sentences it appeared in, so the radiologist can judge it in place."""

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    lexicon_set_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """The lexicon version an approval created."""


class LexiconWatchState(Base, TenantScoped):
    """How far the term watcher has read a lab's edit events."""

    __tablename__ = "lexicon_watch_state"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    last_edit_event_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
