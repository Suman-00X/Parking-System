"""Tables for the people and the clinical context a report belongs to.

Defines: staff accounts (AppUser) and their sign-in refresh tokens (LabRefreshToken), a
radiologist's dictation settings (RadiologistProfile), and who the report is about (Patient, Study).
"""

from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Date, DateTime, Index, LargeBinary, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import MetadataSource, Sex, StudyPriority, UserRole
from radreport.db.base import Base, TenantScoped, TimestampMixin, array_enum_check, enum_check, tenant_fk, tenant_table_args, uuid_pk


class AppUser(Base, TenantScoped, TimestampMixin):
    """Roles are additive."""

    __tablename__ = "app_user"
    __table_args__ = tenant_table_args(
        UniqueConstraint("tenant_id", "employee_code"),
        UniqueConstraint("tenant_id", "email"),
        # Roles are additive, so this is containment rather than equality.
        array_enum_check("roles", UserRole.values()),
        Index("ix_app_user_tenant_active", "tenant_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    employee_code: Mapped[str] = mapped_column(Text, nullable=False)
    """Hospital HR identifier. Unique per tenant, not globally — two labs may legitimately use the same scheme."""

    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """scrypt, as `scrypt$n$r$p$salt$hash`. NULL means the user cannot sign in yet."""

    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LabRefreshToken(Base, TenantScoped):
    """One refresh token for a signed-in lab user; only its hash is stored."""

    __tablename__ = "lab_refresh_token"
    __table_args__ = tenant_table_args(tenant_fk("app_user_id", "app_user", ondelete="CASCADE"), Index("ix_lab_refresh_token_hash", "token_hash", unique=True), Index("ix_lab_refresh_token_user", "tenant_id", "app_user_id", "expires_at"), Index("ix_lab_refresh_token_family", "tenant_id", "family_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    app_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    """Every token rotated from one sign-in shares it, so a replayed old token revokes the whole chain."""

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)


class RadiologistProfile(Base, TenantScoped, TimestampMixin):
    """Speech-specific configuration, one row per dictating radiologist."""

    __tablename__ = "radiologist_profile"
    __table_args__ = tenant_table_args(tenant_fk("user_id", "app_user", ondelete="RESTRICT"), UniqueConstraint("user_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    voice_embedding: Mapped[list[float] | None] = mapped_column(Vector(192), nullable=True)
    """Speaker enrollment for diarization. Biometric data under DPDP."""

    voice_enrolled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voice_consent_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    """/: consent for *enrollment* (diarization)."""

    training_consent_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    """A **separate** consent."""

    default_language: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en-IN'"))
    subspecialty: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    autonomy_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    asr_adapter_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Per-speaker fine-tuned adapter (GA).: this path needs only ≥5 h and the radiologist's own consent — no cross-tenant pooling at all."""


class Patient(Base, TenantScoped, TimestampMixin):
    """Minimal PHI; never sent to third-party APIs."""

    __tablename__ = "patient"
    __table_args__ = tenant_table_args(UniqueConstraint("tenant_id", "mrn"), UniqueConstraint("tenant_id", "pseudonym"), enum_check("sex", Sex.values()))

    id: Mapped[uuid.UUID] = uuid_pk()
    mrn: Mapped[str] = mapped_column(Text, nullable=False)
    name_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pseudonym: Mapped[str] = mapped_column(Text, nullable=False)
    date_of_birth: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    age_years: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    sex: Mapped[str | None] = mapped_column(String(1), nullable=True)


class Study(Base, TenantScoped, TimestampMixin):
    """The imaging examination."""

    __tablename__ = "study"
    __table_args__ = tenant_table_args(tenant_fk("patient_id", "patient", ondelete="RESTRICT"), UniqueConstraint("tenant_id", "accession_number"), UniqueConstraint("tenant_id", "study_instance_uid"), enum_check("metadata_source", MetadataSource.values()), enum_check("priority", StudyPriority.values()), Index("ix_study_patient_datetime", "tenant_id", "patient_id", "study_datetime"), Index("ix_study_description", "tenant_id", "study_description"))

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    accession_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    """RIS linkage key. Confirmed per-report, **not** chained across a patient's studies — do not use it for prior-study linkage."""

    study_instance_uid: Mapped[str | None] = mapped_column(Text, nullable=True)
    study_datetime: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    referring_doctor: Mapped[str | None] = mapped_column(Text, nullable=True)

    modality: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_part_examined: Mapped[str | None] = mapped_column(Text, nullable=True)
    study_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    protocol_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    contrast_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    metadata_source: Mapped[str] = mapped_column(String(16), nullable=False, server_default=MetadataSource.UPLOAD)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, server_default=StudyPriority.ROUTINE)
    """Requires priority ordering. Also the batching boundary: stat/urgent never goes on the Batch API."""

    prior_study_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=True)
    """Deferred — on hold pending a customer request and a real patient-identity linkage strategy."""

    duration_hint_seconds: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
